"""只使用本机固定权重的联合编码重排，不决定正文许可或检索范围。

调用方必须先完成同范围过滤、片段去重，再传入最多20条候选。输出仅改变
顺序并增加裸相关性 logit；不是概率、正文真实性或工程适用性判断。
本模块没有下载、远程推理或默认检索切换入口。
"""

import copy
import hashlib
import json
import math
import time
from importlib.metadata import version
from pathlib import Path

MODEL_NAME = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
MODEL_COMMIT = "1427fd652930e4ba29e8149678df786c240d8825"
MODEL_FILE = "onnx/model_quint8_avx2.onnx"
MODEL_FILES = {
    MODEL_FILE: "6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b",
    "config.json": "cc2cfe51aa3fd759d21d21acf5dfd6994aa67a3c9210636d22e143699d336c77",
    "tokenizer.json": "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626",
    "tokenizer_config.json": "e7fbfbfa6347b4e414c1cee50d142e2c2f9a895dad68b068ae83a8b564c3837e",
    "special_tokens_map.json": "378eb3bf733eb16e65792d7e3fda5b8a4631387ca04d2015199c4d4f22ae554d",
    "README.md": "474736a65d6393a060119a8dc304563af67af4d8d86ccfee4a05dd0df107fc11",
}
PAIR_LIMIT = 20


class RerankerIntegrityError(ValueError):
    """固定模型、图合同或模型输出不完整；拒绝返回部分成功。"""


class RerankerBudgetError(ValueError):
    """输入或耗时超过本轮明确预算，不静默裁掉候选。"""


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()


def _local_manifest(model_dir):
    """先逐个流式核完整SHA，再准许运行库读取；不触发任何下载。"""
    requested = Path(model_dir)
    if requested.is_symlink() or not requested.is_dir():
        raise RerankerIntegrityError("固定本地模型目录缺失或是符号链接")
    root = requested.resolve()
    files = {}
    for name, expected in MODEL_FILES.items():
        path = root / name
        # 也拒绝onnx子目录链接，避免固定文件检查穿过可替换的外部路径。
        components = [path, *list(path.parents)[:len(Path(name).parts) - 1]]
        if (any(item.is_symlink() for item in components)
                or not path.is_file() or not path.resolve().is_relative_to(root)):
            raise RerankerIntegrityError(f"固定本地模型文件缺失或不是普通文件：{name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise RerankerIntegrityError(f"固定本地模型文件哈希不匹配：{name}")
        files[name] = {"sha256": digest, "size_bytes": path.stat().st_size}
    return root, {"model_name": MODEL_NAME, "commit": MODEL_COMMIT,
                  "license": "apache-2.0", "files": files}


def _graph_contract(session):
    """核实本下载件实际I/O，而不是为所有BERT导出臆造token_type_ids。"""
    inputs = session.get_inputs()
    if (len(inputs) != 2
            or {item.name for item in inputs} != {"input_ids", "attention_mask"}
            or any(item.type != "tensor(int64)" or len(item.shape) != 2
                   or (isinstance(item.shape[0], int) and item.shape[0] != 1)
                   for item in inputs)):
        raise RerankerIntegrityError("重排图输入必须是int64的input_ids和attention_mask")
    outputs = session.get_outputs()
    if (len(outputs) != 1 or outputs[0].name != "logits"
            or outputs[0].type != "tensor(float)" or len(outputs[0].shape) != 2
            or outputs[0].shape[1] != 1):
        raise RerankerIntegrityError("重排图输出必须是每对一个float logit")
    if session.get_providers() != ["CPUExecutionProvider"]:
        raise RerankerIntegrityError("本候选只允许明确的CPU运行环境")
    return {"inputs": [{"name": item.name, "type": item.type, "shape": item.shape}
                       for item in inputs],
            "outputs": [{"name": item.name, "type": item.type, "shape": item.shape}
                        for item in outputs], "providers": session.get_providers()}


class PinnedOnnxReranker:
    """固定文件与参数的真实ONNX评分器，批次为1，最多20对。"""

    @classmethod
    def from_local(cls, model_dir, *, max_length=512, threads=2, time_budget_seconds=30,
                   cpu_memory_arena=True, memory_pattern=True, graph_optimization="ALL"):
        if type(max_length) is not int or not 8 <= max_length <= 512:
            raise RerankerBudgetError("token上限必须是8到512的整数")
        if type(threads) is not int or not 1 <= threads <= 2:
            raise RerankerBudgetError("CPU线程预算必须是1或2")
        if (type(time_budget_seconds) not in (int, float)
                or not math.isfinite(time_budget_seconds) or time_budget_seconds <= 0):
            raise RerankerBudgetError("总时间预算必须是有限正数")
        if type(cpu_memory_arena) is not bool or type(memory_pattern) is not bool:
            raise RerankerBudgetError("ORT内存开关必须为明确布尔值")
        if (not isinstance(graph_optimization, str)
                or graph_optimization not in {"ALL", "BASIC", "DISABLED"}):
            raise RerankerBudgetError("图优化只能使用ALL、BASIC或DISABLED")
        root, manifest = _local_manifest(model_dir)
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        tokenizer_config = json.loads((root / "tokenizer_config.json").read_text(encoding="utf-8"))
        if (config.get("model_type") != "xlm-roberta"
                or config.get("architectures") != ["XLMRobertaForSequenceClassification"]
                or config.get("sbert_ce_default_activation_function")
                != "torch.nn.modules.linear.Identity"
                or tokenizer_config.get("model_max_length") != 512):
            raise RerankerIntegrityError("固定模型或分词配置不支持声明的联合logit合同")

        # 本地模型载入没有网络能力；不引入torch/transformers等生产依赖。
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        # 保持原默认；只为有界资源比较显式传入其他设置，参数进入独立指纹。
        options.enable_cpu_mem_arena = cpu_memory_arena
        options.enable_mem_pattern = memory_pattern
        options.graph_optimization_level = {
            "ALL": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
            "BASIC": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
            "DISABLED": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
        }[graph_optimization]
        session = ort.InferenceSession(str(root / MODEL_FILE), sess_options=options,
                                       providers=["CPUExecutionProvider"])
        io_contract = _graph_contract(session)
        tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=max_length, stride=0,
                                     strategy="longest_first", direction="right")
        instance = cls()
        instance._session = session
        instance._tokenizer = tokenizer
        instance._clock = time.monotonic
        instance._manifest = {**manifest, "io_contract": io_contract,
                              "runtime": {name: version(name) for name in
                                          ("onnxruntime", "tokenizers", "numpy")}}
        instance.model_fingerprint = _fingerprint(instance._manifest)
        instance._parameters = {
            "max_length": max_length, "batch_size": 1, "threads": threads,
            "inter_op_threads": 1, "execution_mode": "ORT_SEQUENTIAL",
            "time_budget_seconds": time_budget_seconds, "pair_limit": PAIR_LIMIT,
            "ort_session_options": {"cpu_memory_arena": cpu_memory_arena,
                                    "memory_pattern": memory_pattern,
                                    "graph_optimization": graph_optimization},
            "truncation": {"strategy": "longest_first", "direction": "right", "stride": 0},
            "add_special_tokens": True, "score_activation": "Identity",
            "tokenizer_sha256": manifest["files"]["tokenizer.json"]["sha256"],
            "tokenizer_config_sha256": manifest["files"]["tokenizer_config.json"]["sha256"],
        }
        instance.parameters_fingerprint = _fingerprint(instance._parameters)
        return instance

    @property
    def manifest(self):
        return copy.deepcopy(self._manifest)

    @property
    def parameters(self):
        return copy.deepcopy(self._parameters)

    def _check_deadline(self, started):
        # 在原生调用前后检查，无法抢占已运行的ORT；调用方仍需隔离超时监督。
        if self._clock() - started > self._parameters["time_budget_seconds"]:
            raise RerankerBudgetError("本轮重排超过总时间预算；不返回部分评分")

    def score_pairs(self, query, passages):
        """联合编码每个(query,passage)，返回原始logit和可审计资源参数。"""
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise RerankerBudgetError("重排问题必须为1到500字符的非空文字")
        if not isinstance(passages, (list, tuple)) or len(passages) > PAIR_LIMIT:
            raise RerankerBudgetError("重排最多接收20对，不接受流式隐性裁剪")
        if any(not isinstance(text, str) or not text.strip() for text in passages):
            raise RerankerIntegrityError("重排候选正文必须为非空文字")
        import numpy as np

        started = self._clock()
        scores, token_counts, truncated_pairs = [], [], []
        for index, passage in enumerate(passages):
            self._check_deadline(started)
            encoded = self._tokenizer.encode(query, passage, add_special_tokens=True)
            self._check_deadline(started)
            ids, mask = encoded.ids, encoded.attention_mask
            if (not ids or len(ids) != len(mask) or len(ids) > self._parameters["max_length"]
                    or any(type(value) is not int or value < 0 for value in ids)
                    or any(type(value) is not int or value not in (0, 1) for value in mask)):
                raise RerankerIntegrityError("分词结果不满足固定输入合同")
            token_counts.append(len(ids))
            if encoded.overflowing:
                truncated_pairs.append(index)
            feeds = {"input_ids": np.asarray([ids], dtype=np.int64),
                     "attention_mask": np.asarray([mask], dtype=np.int64)}
            raw = self._session.run(["logits"], feeds)
            self._check_deadline(started)
            if (not isinstance(raw, (list, tuple)) or len(raw) != 1
                    or not isinstance(raw[0], np.ndarray) or raw[0].shape != (1, 1)
                    or raw[0].dtype != np.float32):
                raise RerankerIntegrityError("模型必须返回一对一个float32分数")
            score = float(raw[0][0, 0])
            if not math.isfinite(score):
                raise RerankerIntegrityError("模型输出必须为有限数值")
            scores.append(score)
        return {"status": "COMPLETED" if passages else "NOT_RUN_EMPTY",
                "scores": scores, "input_count": len(passages), "scored_count": len(scores),
                "score_kind": "RAW_RELEVANCE_LOGIT", "model_fingerprint": self.model_fingerprint,
                "parameters_fingerprint": self.parameters_fingerprint,
                "parameters": self.parameters, "token_counts": token_counts,
                "truncated_pair_indexes": truncated_pairs,
                "elapsed_ms": max(0, (self._clock() - started) * 1000),
                "deadline_enforcement": "CHECK_BEFORE_AND_AFTER_NATIVE_CALL_NOT_PREEMPTIVE"}

    def rerank(self, query, candidates, top_k=5):
        """保持全部原文、页码及出处字段；不自行搜索、授权或合并不同片段。"""
        if type(top_k) is not int or not 1 <= top_k <= 5:
            raise RerankerBudgetError("核心候选数必须为1到5的整数")
        if not isinstance(candidates, (list, tuple)) or len(candidates) > PAIR_LIMIT:
            raise RerankerBudgetError("融合候选必须先去重且不超过20条")
        identities = set()
        for row in candidates:
            if not isinstance(row, dict) or any(
                    not isinstance(row.get(key), str) or not row[key].strip()
                    for key in ("id", "source_id", "snapshot_id", "text")):
                raise RerankerIntegrityError("候选缺少来源、快照、片段身份或正文")
            identity = (row["source_id"], row["snapshot_id"], row["id"])
            if identity in identities:
                raise RerankerBudgetError("融合候选尚未按来源、快照和片段身份去重")
            identities.add(identity)
        answer = self.score_pairs(query, [row["text"] for row in candidates])
        ordered = sorted(enumerate(answer["scores"]), key=lambda pair: (-pair[1], pair[0]))
        results = [{**copy.deepcopy(candidates[index]), "rerank_score": score}
                   for index, score in ordered[:top_k]]
        return {**answer, "results": results, "requested_top_k": top_k,
                "tie_break": "ORIGINAL_FUSED_RANK"}
