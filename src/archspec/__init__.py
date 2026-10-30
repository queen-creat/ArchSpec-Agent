"""ArchSpec Agent 后端程序包。"""

import os

# 在OCR、向量检索或重排首次导入ONNX Runtime之前关闭1DS后台遥测。
# 该上传线程曾在macOS退出时发生原生互斥锁崩溃；不靠强制退出来掩盖。
# 此项只影响遥测，不加载模型，也不改变固定正文或推理参数。
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
