"""公开原件的传输节点；业务任务、许可、快照和发布仍归现有流程。"""
from __future__ import annotations
import hashlib,hmac,os,queue,re,tempfile,time
from contextlib import contextmanager
from pathlib import Path
from threading import Event,RLock
from uuid import uuid4
import httpx
from fastapi import APIRouter,HTTPException,Request,Response
from archspec.official_documents.government_urls import validate_government_url, OFFICIAL_TRANSPORT_LIMITS as LIMITS

router=APIRouter(prefix="/api/official-fetch",tags=["官方原件传输"])
class FetchNodeUnavailable(httpx.ConnectError):
    public_message="独立获取节点暂不可用，官方链接保留，原件尚未取得。"

class FetchBroker:
    """仅管理当前HTTP传输；原查询和正文任务沿用已有云端持久记录。"""
    def __init__(self,directory=None):
        self.directory=directory;self.pending=queue.Queue(maxsize=8);self.jobs={};self.lock=RLock();self.seen=0
    def temporary(self):
        from archspec.config import settings
        folder=self.directory or settings.data_dir/".official-fetch-transport"
        Path(folder).mkdir(parents=True,exist_ok=True)
        return tempfile.SpooledTemporaryFile(max_size=4*1024*1024,dir=folder)
    def claim(self,wait):
        self.seen=time.monotonic()
        try:job=self.pending.get(timeout=min(15,max(.05,wait)))
        except queue.Empty:return None
        with self.lock:
            return job if self.jobs.get(job["id"]) is job else None
    @contextmanager
    def response(self,url,maximum):
        if maximum not in LIMITS or time.monotonic()-self.seen>45:raise FetchNodeUnavailable()
        job={"id":str(uuid4()),"url":validate_government_url(url),"max_bytes":maximum,"done":Event(),"result":None}
        try:
            with self.lock:self.jobs[job["id"]]=job
            self.pending.put_nowait(job)
            if not job["done"].wait(120) or not job["result"]:raise FetchNodeUnavailable()
            status,headers,file=job["result"]
            yield httpx.Response(status,headers=headers,stream=_FileStream(file),request=httpx.Request("GET",url))
        except queue.Full:raise FetchNodeUnavailable() from None
        finally:
            with self.lock:self.jobs.pop(job["id"],None)
            if job["result"]:job["result"][2].close()
class _FileStream(httpx.SyncByteStream):
    def __init__(self,file):self.file=file
    def __iter__(self):
        while block:=self.file.read(64*1024):yield block

BROKER=FetchBroker()
def node_mode():
    mode=os.getenv("ARCHSPEC_OFFICIAL_FETCH_MODE","DIRECT")
    if mode not in {"DIRECT","NODE"}:raise ValueError("官方获取模式配置无效")
    return mode=="NODE"
def check_configuration():
    if node_mode() and not re.fullmatch(r"[a-f0-9]{64}",os.getenv("ARCHSPEC_FETCH_WORKER_TOKEN_SHA256","")):
        raise ValueError("独立获取节点的身份校验配置不完整")
def authorize_worker(request):
    check_configuration()
    digest=hashlib.sha256(request.headers.get("X-Archspec-Fetch-Token","").encode()).hexdigest()
    expected=os.getenv("ARCHSPEC_FETCH_WORKER_TOKEN_SHA256","")
    if not node_mode() or not hmac.compare_digest(digest,expected):raise HTTPException(401,"获取节点身份未通过")
@router.get("/status")
def status():return {"mode":"NODE" if node_mode() else "DIRECT","connected":time.monotonic()-BROKER.seen<45}
@router.get("/next")
def next_request(request:Request,wait_seconds:float=15):
    authorize_worker(request);job=BROKER.claim(wait_seconds)
    return {key:job[key] for key in ("id","url","max_bytes")} if job else Response(status_code=204)
@router.post("/{request_id}/response")
async def receive_response(request_id:str,request:Request):
    authorize_worker(request)
    with BROKER.lock:job=BROKER.jobs.get(request_id)
    if job is None:raise HTTPException(410,"传输已结束，未重新提交")
    if request.headers.get("X-Archspec-Fetch-Error"):
        job["done"].set();return {"state":"UNAVAILABLE"}
    status=int(request.headers.get("X-Archspec-Origin-Status","502"))
    headers={}
    for original,name in [("content-type","X-Archspec-Origin-Type"),("location","X-Archspec-Origin-Location")]:
        if value:=request.headers.get(name):headers[original]=value
    file=BROKER.temporary();digest=hashlib.sha256();size=0;published=False
    try:
        async for block in request.stream():
            size+=len(block)
            if size>job["max_bytes"]:raise HTTPException(413,"公开原件超过既有范围")
            digest.update(block);file.write(block)
        if digest.hexdigest()!=request.headers.get("X-Archspec-Original-SHA256"):
            raise HTTPException(400,"原件传输字节尚未核齐")
        file.seek(0)
        with BROKER.lock:
            if BROKER.jobs.get(request_id) is not job:raise HTTPException(410,"传输已结束")
            job["result"]=(status,headers,file);published=True;job["done"].set()
        return {"state":"RECEIVED","bytes":size,"sha256":digest.hexdigest()}
    finally:
        if not published:file.close();job["done"].set()
@contextmanager
def open_public_response(client,url,maximum,**options):
    # 已审核的非政府附件继续原读取边界；节点只接受公开政府URL。
    from urllib.parse import urlsplit
    if node_mode() and (urlsplit(url).hostname or "").endswith(".gov.cn"):
        check_configuration()
        with BROKER.response(url,maximum) as response:yield response
    else:
        with client.stream("GET",url,**options) as response:yield response
