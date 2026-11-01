import json
import os
from pathlib import Path
import tempfile
import unittest

import httpx
from corex_clips_mcp.client import Clips, ClientError, catalog

TOKEN='cxclips_mcp_f_'+'a'*64
R2='https://'+'b'*32+'.r2.cloudflarestorage.com/bucket/video?X-Amz-Signature=fixture'
VIDEO=b'\x00\x00\x00\x18ftypisom'+b'\x00'*128


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.media=self.root/'media'; self.media.mkdir()
        self.file=self.media/'video.mp4'; self.file.write_bytes(VIDEO)
        self.calls=[]; self.puts=[]; self.started={'mode':'simple','key':'Videos/ws/uuid/video.mp4','uploadUrl':R2,'headers':{'Content-Type':'video/mp4'}}
        self.completion={'id':'video-1','status':'UPLOADED'}
        self.handler=self.respond

    def respond(self,request):
        if request.method=='PUT':
            self.puts.append((str(request.url),dict(request.headers),request.content))
            return httpx.Response(200,headers={'ETag':'"etag-1"'})
        body=json.loads(request.content); name=body.get('params',{}).get('name')
        if name=='clips_upload_start': result=self.started
        elif name=='clips_upload_part_url': result={'uploadUrl':R2+'&part='+str(body['params']['arguments']['partNumber'])}
        elif name=='clips_upload_complete': result=self.completion
        else: result={'ok':True}
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':{'content':[{'type':'text','text':json.dumps(result)}],'isError':False}})

    def client(self,token=TOKEN,root=True):
        def dispatch(request):
            self.calls.append(request)
            return self.handler(request)
        client=Clips(credentials=self.root/'private/config.json',token=token,base='https://clips.test',
                     upload_root=self.media if root else None,transport=httpx.MockTransport(dispatch))
        self.addCleanup(client.close); return client

    def args(self,**extra):
        return {'file_path':str(self.file),'projectId':'project-1','confirmed':True,'captionsEnabled':False,**extra}

    def names(self):
        return [json.loads(r.content)['params'].get('name') for r in self.calls if r.method=='POST']

    def test_scope_filter_and_safe_tokens(self):
        self.assertEqual(len(catalog()),52)
        self.assertEqual(len(self.client().operations),53)
        read=self.client(token='cxclips_mcp_1_'+'b'*64)
        self.assertNotIn('clips_upload_file',read.operations)
        self.assertNotIn('clips_upload_start',read.operations)
        self.assertIn('clips_list_videos',read.operations)
        for token in ['jwt','cxclips_live_'+'a'*64,'cxclips_mcp_0_'+'a'*64,TOKEN+'\r\nother']:
            with self.assertRaises(ClientError): Clips(credentials=self.root/'none',token=token)

    def test_simple_upload_uses_snapshot_and_no_mcp_auth_on_storage(self):
        result=self.client().call('clips_upload_file',self.args())
        self.assertEqual(result['structuredContent']['video']['id'],'video-1')
        self.assertFalse(result['structuredContent']['processing_complete'])
        self.assertEqual(self.names(),['clips_upload_start','clips_upload_complete'])
        self.assertEqual(len(self.puts),1); self.assertEqual(self.puts[0][2],VIDEO)
        self.assertNotIn('authorization',self.puts[0][1]); self.assertNotIn('cookie',self.puts[0][1])
        self.assertEqual(self.puts[0][1]['content-length'],str(len(VIDEO)))
        self.assertNotIn('transfer-encoding',self.puts[0][1])
        request=json.loads(self.calls[-1].content)['params']['arguments']
        self.assertFalse(request['captionsEnabled']); self.assertTrue(request['confirmed'])
        receipt=Path(result['structuredContent']['receipt_file']); saved=receipt.read_text()
        self.assertEqual(json.loads(saved)['state'],'registered')
        self.assertNotIn(TOKEN,saved); self.assertNotIn('X-Amz',saved)
        self.assertEqual(list((self.root/'private/upload-cache').iterdir()),[])

    def test_multipart_exact_parts_and_etags(self):
        size=5*1024*1024
        content=VIDEO+b'z'*(size*2+7-len(VIDEO)); self.file.write_bytes(content)
        self.started={'mode':'multipart','key':'Videos/ws/uuid/video.mp4','uploadId':'upload-1','partSizeBytes':size,'totalParts':3}
        result=self.client().call('clips_upload_file',self.args())
        self.assertEqual([len(p[2]) for p in self.puts],[size,size,7])
        for _,headers,body in self.puts:
            self.assertEqual(headers['content-length'],str(len(body)))
            self.assertNotIn('transfer-encoding',headers)
        self.assertEqual(b''.join(p[2] for p in self.puts),content)
        complete=json.loads(self.calls[-1].content)['params']['arguments']
        self.assertEqual(complete['uploadId'],'upload-1')
        self.assertEqual(complete['parts'],[{'part_number':n,'etag':'"etag-1"'} for n in [1,2,3]])
        self.assertEqual(json.loads(Path(result['structuredContent']['receipt_file']).read_text())['state'],'registered')

    def test_missing_confirmation_and_unknown_fields_send_nothing(self):
        client=self.client()
        for args in [self.args(confirmed=False),self.args(workspaceId='other')]:
            with self.assertRaises(ClientError): client.call('clips_upload_file',args)
        self.assertEqual(self.calls,[])

    def test_outside_hidden_empty_bad_header_and_size(self):
        client=self.client()
        outside=self.root/'outside.mp4'; outside.write_bytes(VIDEO)
        hidden=self.media/'.hidden.mp4'; hidden.write_bytes(VIDEO)
        empty=self.media/'empty.mp4'; empty.write_bytes(b'')
        wrong=self.media/'wrong.mp4'; wrong.write_bytes(b'not video')
        text=self.media/'secret.txt'; text.write_bytes(VIDEO)
        for path in [outside,hidden,empty,wrong,text]:
            with self.assertRaises(ClientError): client.call('clips_upload_file',self.args(file_path=str(path)))
        client.MAX_UPLOAD=1
        with self.assertRaises(ClientError): client.call('clips_upload_file',self.args())
        self.assertEqual(self.calls,[])

    def test_missing_root_sends_nothing(self):
        with self.assertRaises(ClientError): self.client(root=False).call('clips_upload_file',self.args())
        self.assertEqual(self.calls,[])

    def test_hardlink_refused(self):
        link=self.media/'linked.mp4'; os.link(self.file,link)
        with self.assertRaises(ClientError): self.client().call('clips_upload_file',self.args(file_path=str(link)))
        self.assertEqual(self.calls,[])

    def test_symlink_refused(self):
        link=self.media/'link.mp4'
        try: link.symlink_to(self.file)
        except OSError: self.skipTest('Windows symlink privilege unavailable')
        with self.assertRaises(ClientError): self.client().call('clips_upload_file',self.args(file_path=str(link)))
        self.assertEqual(self.calls,[])

    def test_untrusted_storage_urls_never_receive_bytes(self):
        client=self.client()
        for url in ['https://evil.test/upload','http://127.0.0.1/upload','https://user:pass@'+'b'*32+'.r2.cloudflarestorage.com/x',
                    'https://'+'b'*32+'.r2.cloudflarestorage.com:444/x',R2+'#fragment']:
            self.started['uploadUrl']=url
            with self.assertRaises(ClientError): client.call('clips_upload_file',self.args())
        self.assertEqual(self.puts,[])

    def test_server_cannot_forward_authorization_header_to_storage(self):
        self.started['headers']={'Authorization':'secret'}
        with self.assertRaises(ClientError): self.client().call('clips_upload_file',self.args())
        self.assertEqual(self.puts,[])

    def test_lost_storage_reply_is_not_retried_or_completed(self):
        original=self.respond
        def handler(request):
            if request.method=='PUT': raise httpx.ReadTimeout('sensitive URL',request=request)
            return original(request)
        self.handler=handler
        with self.assertRaises(ClientError) as raised: self.client().call('clips_upload_file',self.args())
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(self.names(),['clips_upload_start'])
        self.assertEqual(sum(r.method=='PUT' for r in self.calls),1)
        self.assertEqual(json.loads(Path(raised.exception.receipt).read_text())['state'],'uncertain')
        self.assertNotIn('sensitive',str(raised.exception))

    def test_lost_completion_reply_is_not_retried(self):
        original=self.respond
        def handler(request):
            if request.method=='POST' and json.loads(request.content)['params']['name']=='clips_upload_complete':
                raise httpx.ReadTimeout('sensitive',request=request)
            return original(request)
        self.handler=handler
        with self.assertRaises(ClientError) as raised: self.client().call('clips_upload_file',self.args())
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(self.names(),['clips_upload_start','clips_upload_complete'])
        self.assertEqual(json.loads(Path(raised.exception.receipt).read_text())['state'],'uncertain')

    def test_invalid_multipart_plan_and_missing_video_id_are_not_success(self):
        self.started={'mode':'multipart','key':'Videos/ws/id/video.mp4','uploadId':'id','partSizeBytes':1,'totalParts':1}
        with self.assertRaises(ClientError): self.client().call('clips_upload_file',self.args())
        self.assertEqual(self.puts,[])
        self.started={'mode':'simple','key':'Videos/ws/id/video.mp4','uploadUrl':R2,'headers':{}}
        self.completion={}
        with self.assertRaises(ClientError) as raised: self.client().call('clips_upload_file',self.args())
        self.assertTrue(raised.exception.uncertain)

    def test_redirect_and_wrong_rpc_identity_refused(self):
        self.handler=lambda request:httpx.Response(302,headers={'Location':'https://elsewhere.test'})
        with self.assertRaises(ClientError): self.client().call('clips_list_projects',{})
        self.assertEqual(len(self.calls),1)
        self.handler=lambda request:httpx.Response(200,json={'jsonrpc':'2.0','id':'other','result':{}})
        with self.assertRaises(ClientError): self.client().call('clips_list_projects',{})

    def test_tokens_in_results_are_redacted(self):
        def handler(request):
            body=json.loads(request.content)
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':{'content':[{'type':'text','text':TOKEN}],'token':TOKEN}})
        self.handler=handler
        self.assertNotIn(TOKEN,json.dumps(self.client().call('clips_list_projects',{})))


if __name__=='__main__': unittest.main()
