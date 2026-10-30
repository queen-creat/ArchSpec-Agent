"""健康检查接口：用于判断后端程序是否已经正常启动。"""

from fastapi import APIRouter

from archspec.official_documents.runtime_ocr import runtime_ocr_state

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def get_health() -> dict[str, str]:
    """返回固定状态；UP 表示后端能够正常接收请求。

    runtime_ocr只报告运行期识别是否开启，便于只读核对小内存实例的部署能力，
    不泄露任何配置值、账号或密钥。
    """
    return {"status": "UP", "service": "archspec-agent", "runtime_ocr": runtime_ocr_state()}

