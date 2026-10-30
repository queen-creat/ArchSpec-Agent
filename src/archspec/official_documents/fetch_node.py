"""独立获取节点只接公开URL；不接用户问题、模型密钥、许可或发布权限。"""
import hashlib,tempfile
from pathlib import Path
from archspec.official_documents.government_urls import validate_government_url, OFFICIAL_TRANSPORT_LIMITS as LIMITS

def run_once(api,upstream,token,*,wait_seconds=15,directory=None):
    auth={"X-Archspec-Fetch-Token":token}
    response=api.get("/api/official-fetch/next",params={"wait_seconds":wait_seconds},headers=auth)
    if response.status_code==204:return None
    response.raise_for_status();job=response.json();url=validate_government_url(job["url"])
    if job["max_bytes"] not in LIMITS:raise ValueError("获取范围无效")
    folder=Path(directory) if directory else None
    if folder:folder.mkdir(parents=True,exist_ok=True)
    prefix="/api/official-fetch/"+job["id"]+"/response"
    with tempfile.SpooledTemporaryFile(max_size=4*1024*1024,dir=folder) as file:
        digest=hashlib.sha256();size=0
        try:
            # 调用网站的认证与节点口令绝不发送给官方来源。
            with upstream.stream("GET",url,timeout=30,follow_redirects=False,
                    headers={"User-Agent":"ArchSpec-Agent/0.1 official-copy","Accept-Encoding":"identity"}) as original:
                status=original.status_code
                origin_headers={"X-Archspec-Origin-Type":original.headers.get("content-type","application/octet-stream")}
                if location:=original.headers.get("location"):origin_headers["X-Archspec-Origin-Location"]=location
                for block in original.iter_bytes():
                    size+=len(block)
                    if size>job["max_bytes"]:raise ValueError("公开原件超过既有范围")
                    digest.update(block);file.write(block)
        except Exception:
            result=api.post(prefix,headers={**auth,"X-Archspec-Fetch-Error":"OFFICIAL_SOURCE_UNAVAILABLE"},content=b"")
            result.raise_for_status();return {"id":job["id"],"state":"UNAVAILABLE"}
        file.seek(0)
        def blocks():
            while block:=file.read(64*1024):yield block
        result=api.post(prefix,headers={**auth,**origin_headers,"X-Archspec-Origin-Status":str(status),
            "X-Archspec-Original-SHA256":digest.hexdigest(),"Content-Type":"application/octet-stream",
            "Content-Length":str(size)},content=blocks())
        # 不因响应不明重放原件POST；原请求及后续读取决定真实状态。
        result.raise_for_status()
        return {"id":job["id"],"url":url,"state":"DELIVERED","bytes":size,"sha256":digest.hexdigest()}


def run_worker(api, upstream, token, stop, *, wait_seconds=15, directory=None, on_event=None):
    """常驻领取既有队列；暂时不可用只报告状态，不重放原件回传。"""
    while not stop.is_set():
        try:
            event = run_once(api, upstream, token, wait_seconds=wait_seconds, directory=directory)
            event = event or {"state": "IDLE"}
        except Exception as error:
            event = {"state": "UNAVAILABLE", "type": type(error).__name__}
        if on_event:
            on_event(event)
        if event["state"] == "UNAVAILABLE":
            stop.wait(2)
