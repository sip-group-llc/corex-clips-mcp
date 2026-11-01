"""Transport only. Video processing and accounting remain on the private server."""
from contextlib import contextmanager
import hashlib
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit
import uuid

import httpx
import jsonschema
from .private import ensure_private_directory, write_private_json

SCOPES = ('read', 'edit', 'generate', 'render')
MIME = {'.mp4':'video/mp4', '.m4v':'video/mp4', '.mov':'video/quicktime',
        '.webm':'video/webm', '.mkv':'video/x-matroska', '.avi':'video/x-msvideo',
        '.mpeg':'video/mpeg', '.mpg':'video/mpeg'}
DEFAULT_CREDENTIALS = (Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'CoreXClips/mcp/credentials.json'
                       if os.name == 'nt' else Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'corex-clips-mcp/credentials.json')


def credential_path(explicit=None):
    return Path(explicit or os.environ.get('COREX_CLIPS_CREDENTIALS_FILE') or DEFAULT_CREDENTIALS).expanduser()


def catalog():
    return json.loads(files('corex_clips_mcp').joinpath('catalog.json').read_text(encoding='utf-8'))['tools']


def sanitize(value, token=''):
    if isinstance(value, dict):
        return {k:'[REDACTED]' if str(k).lower() in {'authorization','token','password','secret','api_key','access_token','refresh_token'}
                else sanitize(v,token) for k,v in value.items()}
    if isinstance(value,list): return [sanitize(v,token) for v in value]
    if isinstance(value,str):
        if token: value=value.replace(token,'[REDACTED]')
        return re.sub(r'cxclips_(?:mcp_[1-9a-f]_|live_)[a-f0-9]{64}', '[REDACTED]', value)
    return value


class ClientError(Exception):
    def __init__(self, code, message, *, uncertain=False, receipt=None):
        super().__init__(message)
        self.code,self.message,self.uncertain,self.receipt=code,message,uncertain,receipt

    def result(self):
        data={'code':self.code,'message':self.message,'outcome_uncertain':self.uncertain}
        if self.receipt: data['receipt_file']=str(self.receipt)
        return {'isError':True,'structuredContent':data,'content':[{'type':'text','text':json.dumps(data)}]}


class Clips:
    MAX_RESPONSE = 1_100_000
    MAX_UPLOAD = 15_000_000_000

    def __init__(self, *, credentials=None, base=None, token=None, upload_root=None, transport=None):
        self.credentials=credential_path(credentials)
        saved=json.loads(self.credentials.read_text(encoding='utf-8-sig')) if self.credentials.is_file() else {}
        selected=base or saved.get('base_url') or 'https://clips.corex11.tech'
        if not isinstance(selected,str): raise ClientError('configuration','Invalid Clips origin.')
        self.base=selected.rstrip('/')
        try:
            url=urlsplit(self.base); url.port
        except (ValueError,TypeError): raise ClientError('configuration','Invalid Clips origin.') from None
        if (not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('','/')
            or not (url.scheme=='https' or url.scheme=='http' and url.hostname in {'localhost','127.0.0.1'})):
            raise ClientError('configuration','Use HTTPS, or loopback HTTP for isolated tests.')
        self.token=token or saved.get('token') or ''
        if not isinstance(self.token,str) or not re.fullmatch(r'cxclips_mcp_[1-9a-f]_[a-f0-9]{64}',self.token):
            raise ClientError('credentials','Configure a scoped MCP key. Broad integration keys and login tokens are not accepted.')
        mask=int(self.token.split('_')[2],16)
        self.scopes={scope for bit,scope in enumerate(SCOPES) if mask & 1<<bit}
        self.upload_root=Path(upload_root or saved['upload_root']).expanduser() if upload_root or saved.get('upload_root') else None
        self.operations={t['name']:t for t in catalog() if set(t['_meta']['corex-clips/scopes']) <= self.scopes}
        if 'generate' in self.scopes:
            start=self.operations['clips_upload_start']['inputSchema']
            schema={'type':'object','properties':{k:v for k,v in start['properties'].items() if k not in {'filename','contentType','sizeBytes'}},
                    'required':['projectId','confirmed','file_path'],'additionalProperties':False}
            schema['properties']['file_path']={'type':'string','minLength':1}
            self.operations['clips_upload_file']={'name':'clips_upload_file','description':
                'Upload one user-confirmed local video from upload_root, then register it for normal Clips processing. May consume credits. '
                'Never retry automatically after uncertain outcomes; inspect the private receipt and project first.',
                'inputSchema':schema,'annotations':{'readOnlyHint':False,'destructiveHint':False,'idempotentHint':False,'openWorldHint':True}}
        self.http=httpx.Client(base_url=self.base,trust_env=False,follow_redirects=False,
                               timeout=httpx.Timeout(120,connect=10,write=180),transport=transport)

    def close(self): self.http.close()

    def rpc(self, method, params=None, *, uncertain=False):
        identifier=uuid.uuid4().hex
        try:
            with self.http.stream('POST','/api/mcp',json={'jsonrpc':'2.0','id':identifier,'method':method,'params':params or {}},
                                 headers={'Authorization':'Bearer '+self.token,'Accept':'application/json','MCP-Protocol-Version':'2025-11-25'}) as response:
                body=bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body)>self.MAX_RESPONSE: raise ClientError('response_limit','Response too large.',uncertain=uncertain)
                if response.status_code!=200:
                    raise ClientError('http_'+str(response.status_code),'Clips rejected the request.',
                                      uncertain=uncertain and (response.status_code>=500 or 300<=response.status_code<400))
                try: data=json.loads(body)
                except (ValueError,UnicodeError): raise ClientError('protocol','Invalid server response.',uncertain=uncertain) from None
        except httpx.HTTPError: raise ClientError('transport','Connection failed; inspect state before repeating a write.',uncertain=uncertain) from None
        if not isinstance(data,dict) or data.get('jsonrpc')!='2.0' or data.get('id')!=identifier:
            raise ClientError('protocol','Unexpected response identity.',uncertain=uncertain)
        if 'error' in data: raise ClientError('rpc','Clips refused the operation.')
        if not isinstance(data.get('result'),dict): raise ClientError('protocol','Missing result.',uncertain=uncertain)
        return sanitize(data['result'],self.token)

    def call(self, name, arguments):
        tool=self.operations.get(name)
        if not tool: raise ClientError('scope','Tool unavailable for this credential.')
        try: jsonschema.Draft202012Validator(tool['inputSchema']).validate(arguments)
        except jsonschema.ValidationError as error:
            raise ClientError('arguments','Invalid field: '+'.'.join(map(str,error.absolute_path))) from None
        if name=='clips_upload_file': return self.upload(arguments)
        return self.rpc('tools/call',{'name':name,'arguments':arguments},uncertain=not tool['annotations']['readOnlyHint'])

    def status(self):
        names=set(); cursor=None
        for _ in range(10):
            result=self.rpc('tools/list',{'cursor':cursor} if cursor else {})
            names.update(t.get('name') for t in result.get('tools',[]) if isinstance(t,dict))
            next_cursor=result.get('nextCursor')
            if next_cursor is None: break
            if next_cursor==cursor: raise ClientError('protocol','Repeated catalog cursor.')
            cursor=next_cursor
        else: raise ClientError('protocol','Catalog exceeded pagination limit.')
        return {'remote_tools':len(names),'missing_tools':sorted(set(self.operations)-{'clips_upload_file'}-names),
                'upload_enabled':self.upload_root is not None,'scopes':sorted(self.scopes)}

    def _data(self,name,args):
        result=self.call(name,args)
        if result.get('isError'): raise ClientError('operation','Clips refused this upload stage; inspect the project before retrying.')
        try:
            data=json.loads(next(x['text'] for x in result['content'] if x.get('type')=='text'))
        except (StopIteration,KeyError,ValueError,TypeError):
            raise ClientError('protocol','Incomplete upload result; inspect project state.',uncertain=True) from None
        if not isinstance(data,dict): raise ClientError('protocol','Invalid upload result.',uncertain=True)
        return data

    @contextmanager
    def snapshot(self, filename):
        if self.upload_root is None: raise ClientError('upload_root','Configure a dedicated upload_root first.')
        path=Path(filename).expanduser(); root=self.upload_root
        try:
            if not path.is_absolute() or not root.is_absolute() or '..' in path.parts or str(path).startswith('\\\\'): raise ValueError()
            root=root.resolve(strict=True); relative=path.relative_to(root)
            for item in (path,*path.parents):
                if item==root: break
                if item.is_symlink() or getattr(item,'is_junction',lambda:False)(): raise ValueError()
            if path.resolve(strict=True)!=path or path.suffix.lower() not in MIME: raise ValueError()
            if any(p.startswith('.') or p.lower() in {'credentials','secrets','profiles'} for p in relative.parts): raise ValueError()
            before=path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or not 0<before.st_size<=self.MAX_UPLOAD: raise ValueError()
        except (OSError,ValueError): raise ClientError('file','Use a regular supported video inside upload_root, without links, up to 15 GB.') from None
        cache=ensure_private_directory(self.credentials.parent/'upload-cache')
        with tempfile.TemporaryDirectory(prefix='video-',dir=cache) as folder:
            copy=Path(folder)/'video.bin'; digest=hashlib.sha256(); size=0
            with path.open('rb') as source,copy.open('xb') as dest:
                opened=os.fstat(source.fileno())
                if (opened.st_dev,opened.st_ino,opened.st_size)!=(before.st_dev,before.st_ino,before.st_size):
                    raise ClientError('file_changed','Video changed before copying; nothing was sent.')
                while chunk:=source.read(1024*1024):
                    size+=len(chunk)
                    if size>self.MAX_UPLOAD: raise ClientError('file_changed','Video changed size; nothing was sent.')
                    digest.update(chunk);dest.write(chunk)
                after=os.fstat(source.fileno())
                if size!=before.st_size or after.st_mtime_ns!=before.st_mtime_ns:
                    raise ClientError('file_changed','Video changed while copying; nothing was sent.')
            with copy.open('rb') as stream:
                header=stream.read(64); stream.seek(0); ext=path.suffix.lower()
                valid=(header[4:8] in {b'ftyp',b'moov',b'wide'} if ext in {'.mp4','.m4v','.mov'} else
                       header.startswith(b'\x1aE\xdf\xa3') if ext in {'.mkv','.webm'} else
                       header.startswith(b'RIFF') and header[8:12]==b'AVI ' if ext=='.avi' else
                       header.startswith((b'\x00\x00\x01\xba',b'\x00\x00\x01\xb3')))
                if not valid: raise ClientError('file','Video header does not match its extension; nothing was sent.')
                yield stream,path.name,MIME[ext],size,digest.hexdigest()

    def _put(self,url,headers,stream,length):
        try:
            target=urlsplit(url); port=target.port
        except (TypeError,ValueError): raise ClientError('upload_url','Invalid storage URL.',uncertain=True) from None
        if (target.scheme!='https' or not target.hostname or target.username or target.password or target.fragment
            or port not in (None,443) or not re.fullmatch(r'(?:[a-z0-9-]+\.)?[a-f0-9]{32}\.r2\.cloudflarestorage\.com',target.hostname)):
            raise ClientError('upload_url','Only signed Cloudflare R2 HTTPS upload URLs are accepted.',uncertain=True)
        clean={}
        if not isinstance(headers,dict): raise ClientError('upload_headers','Invalid storage headers.',uncertain=True)
        for key,value in headers.items():
            if key.lower() not in {'content-type','content-length','content-md5','x-amz-checksum-sha256','x-amz-checksum-crc32','x-amz-checksum-crc32c'} or not isinstance(value,str):
                raise ClientError('upload_headers','Unexpected storage header; nothing sent to storage.',uncertain=True)
            if '\r' in value or '\n' in value: raise ClientError('upload_headers','Invalid storage header.',uncertain=True)
            if key.lower()!='content-length': clean[key]=value
        clean['Content-Length']=str(length)
        def chunks():
            left=length
            while left:
                chunk=stream.read(min(left,1024*1024))
                if not chunk: raise ClientError('file_changed','Snapshot ended unexpectedly.',uncertain=True)
                left-=len(chunk); yield chunk
        try:
            # A fresh request carries no MCP Authorization, cookies or inherited proxy settings.
            request=self.http.build_request('PUT',url,headers=clean,content=chunks())
            request.headers.pop('authorization',None); request.headers.pop('cookie',None)
            response=self.http.send(request,stream=True)
            try:
                if response.status_code not in (200,201,204): raise ClientError('storage','Storage refused the upload; no automatic retry.',uncertain=True)
                return response.headers.get('etag')
            finally: response.close()
        except httpx.HTTPError: raise ClientError('storage','Storage connection failed; no automatic retry.',uncertain=True) from None

    def upload(self,args):
        journal=None; state={}
        def save():
            temp=journal.with_name(journal.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                write_private_json(temp,state); os.replace(temp,journal)
            finally: temp.unlink(missing_ok=True)
        try:
            with self.snapshot(args['file_path']) as (stream,name,mime,size,digest):
                journal=ensure_private_directory(self.credentials.parent/'uploads')/(uuid.uuid4().hex+'.json')
                fields={k:v for k,v in args.items() if k!='file_path'}
                fields.update(filename=name,contentType=mime,sizeBytes=size)
                state={'state':'starting','request':fields,'source_sha256':digest,'parts':[]}
                save()
                start=self._data('clips_upload_start',fields)
                key=start.get('key')
                if not isinstance(key,str) or not key: raise ClientError('upload_mode','Direct upload unavailable or incomplete.',uncertain=True)
                state.update(key=key,mode=start.get('mode'),state='uploading'); save()
                complete={**fields,'key':key}
                if start.get('mode')=='simple':
                    self._put(start.get('uploadUrl'),start.get('headers',{}),stream,size)
                elif start.get('mode')=='multipart':
                    part_size=start.get('partSizeBytes'); total=start.get('totalParts'); upload_id=start.get('uploadId')
                    if (type(part_size)!=int or not 5*1024*1024<=part_size<=256*1024*1024 or type(total)!=int
                        or total!=math.ceil(size/part_size) or not 1<=total<=10000 or not isinstance(upload_id,str) or not upload_id):
                        raise ClientError('multipart','Invalid multipart plan.',uncertain=True)
                    state['uploadId']=upload_id; save()
                    for number in range(1,total+1):
                        part=self._data('clips_upload_part_url',{'key':key,'uploadId':upload_id,'sizeBytes':size,'partNumber':number})
                        etag=self._put(part.get('uploadUrl'),{},stream,min(part_size,size-stream.tell()))
                        if not isinstance(etag,str) or not re.fullmatch(r'"?[A-Za-z0-9-]+"?',etag):
                            raise ClientError('etag','Storage did not return a valid part ETag.',uncertain=True)
                        state['parts'].append({'part_number':number,'etag':etag}); save()
                    complete.update(uploadId=upload_id,parts=state['parts'])
                else: raise ClientError('upload_mode','Unsupported upload mode; no fallback to an unrestricted upload.')
                state['state']='completing'; save()
                result=self._data('clips_upload_complete',complete)
                if not isinstance(result.get('id'),str) or not result['id']:
                    raise ClientError('protocol','Missing video identifier; inspect the project before retrying.',uncertain=True)
                state.update(state='registered',video_id=result.get('id')); save()
                data={'video':result,'source_sha256':digest,'receipt_file':str(journal),'processing_complete':False}
                return {'isError':False,'structuredContent':data,'content':[{'type':'text','text':json.dumps(data)}]}
        except ClientError as error:
            if journal:
                state['state']='uncertain' if error.uncertain else 'failed'; state['error_code']=error.code; save()
                error.receipt=journal
            raise
        except Exception:
            if journal:
                state['state']='uncertain'; state['error_code']='local_error'
                try: save()
                except OSError: pass
            raise ClientError('local_error','Local upload failure. Inspect the receipt and project before repeating.',
                              uncertain=journal is not None,receipt=journal) from None
