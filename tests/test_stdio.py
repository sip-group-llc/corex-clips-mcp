import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from corex_clips_mcp.client import catalog
from corex_clips_mcp.private import write_private_json


class StdioProtocolTest(unittest.TestCase):
    def test_actual_stdio_contract_calls_and_redaction(self):
        requests = []
        token = 'cxclips_mcp_f_' + 'a' * 64

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body))
                valid = self.headers.get('Authorization') == 'Bearer ' + token
                result = {'content': [{'type': 'text', 'text': token}],
                          'structuredContent': {'workspaceId': 'fixture', 'token': token}}
                raw = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode()
                self.send_response(200 if valid else 401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proc = None
        try:
            with tempfile.TemporaryDirectory() as folder:
                credential = Path(folder) / 'key.json'
                write_private_json(credential, {'base_url': f'http://127.0.0.1:{server.server_port}', 'token': token})
                proc = subprocess.Popen(
                    [sys.executable, '-I', '-B', '-m', 'corex_clips_mcp', '--credentials', str(credential), 'mcp'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', cwd=folder,
                    env=dict(os.environ, OTEL_SDK_DISABLED='true'))
                messages = queue.Queue()

                def read():
                    for line in proc.stdout:
                        try:
                            messages.put(json.loads(line))
                        except ValueError:
                            messages.put({'invalid_stdout': line})

                reader = threading.Thread(target=read, daemon=True)
                reader.start()

                def send(method, params, identifier=None):
                    payload = {'jsonrpc': '2.0', 'method': method, 'params': params}
                    if identifier is not None:
                        payload['id'] = identifier
                    proc.stdin.write(json.dumps(payload) + '\n')
                    proc.stdin.flush()
                    if identifier is None:
                        return
                    while True:
                        message = messages.get(timeout=30)
                        self.assertNotIn('invalid_stdout', message)
                        if message.get('id') == identifier:
                            return message

                initialized = send('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                   'clientInfo': {'name': 'fixture', 'version': '1'}}, 1)
                self.assertEqual(initialized['result']['serverInfo']['name'], 'corex-clips')
                self.assertEqual(initialized['result']['protocolVersion'], '2025-11-25')
                send('notifications/initialized', {})
                tools = send('tools/list', {}, 2)['result']['tools']
                self.assertEqual({t['name'] for t in tools}, {t['name'] for t in catalog()} | {'clips_upload_file'})
                self.assertEqual(len(tools), 53)
                result = send('tools/call', {'name': 'clips_list_projects', 'arguments': {}}, 3)['result']
                self.assertFalse(result.get('isError'))
                self.assertEqual(result['structuredContent']['workspaceId'], 'fixture')
                self.assertNotIn(token, json.dumps(result))
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][0], '/api/mcp')
                for identifier, name, args in [(4, 'get_balance', {}),
                                               (5, 'clips_list_projects', {'workspaceId': 'foreign'}),
                                               (6, 'clips_upload_file', {'file_path': 'secret.png', 'confirmed': False})]:
                    denied = send('tools/call', {'name': name, 'arguments': args}, identifier)['result']
                    self.assertTrue(denied['isError'])
                self.assertEqual(len(requests), 1)
                proc.stdin.close()
                proc.wait(timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr.read()[-1500:])
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if proc is not None:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
