"""核验明确的题库处理绑定修订；绝不根据搜索结果自动挑选答案。"""

import copy
import hashlib
import json
import re
import struct
from datetime import date
from pathlib import Path

import pymupdf


def reviewed_page_manifest(dataset, project_root):
    """核对实际页图，并返回可复查清单；指纹不能代替人工看原页。"""
    if dataset.get("binding_revision") is None:
        return {}
    root = Path(project_root).resolve()
    reviewed = {}
    try:
        for review in dataset["binding_revision"]["reviews"]:
            for page in review["pages"]:
                name = page["image_path"]
                if not isinstance(name, str) or not name or ":" in name:
                    raise ValueError("核对原页路径无效")
                # 同时考虑Windows和斜线路径，避免父目录或盘符读取个人文件。
                relative = Path(name.replace("\\", "/"))
                if (relative.is_absolute() or ".." in relative.parts
                        or relative.parts[:2] != ("data", "browser-quality-check")
                        or relative.suffix != ".png"):
                    raise ValueError("核对原页路径不在项目验收目录内")
                path = (root / relative).resolve(strict=True)
                # 最终位置也必须留在验收目录；目录连接不能绕到其它个人/项目文件。
                if (not path.is_relative_to(root / "data" / "browser-quality-check")
                        or not path.is_file()):
                    raise ValueError("核对原页图片不在项目内或缺失")
                size = path.stat().st_size
                if not 24 <= size <= 20 * 1024 * 1024:
                    raise ValueError("核对原页图片缺失或超限")
                with path.open("rb") as stream:
                    # 只读一份限额字节；核对后再按路径开文件会遇到文件被替换的竞态。
                    content = stream.read(20 * 1024 * 1024 + 1)
                    if len(content) != size or len(content) > 20 * 1024 * 1024:
                        raise ValueError("核对原页图片读取中发生变化或超限")
                    header = content[:24]
                    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                        raise ValueError("核对原页不是完整PNG图片")
                    width, height = struct.unpack(">II", header[16:24])
                    if not 1 <= width * height <= 16_000_000 or not width or not height:
                        raise ValueError("核对原页图片像素超限")
                    digest = hashlib.sha256(content).hexdigest()
                if digest != page["image_sha256"]:
                    raise ValueError("核对原页图片已改变，不能沿用旧核对记录")
                # 签名及SHA一致还不够；必须能按图片解析，不接收伪造的PNG头。
                pixmap = pymupdf.Pixmap(content)
                if (pixmap.width, pixmap.height) != (width, height):
                    raise ValueError("核对原页图片尺寸不一致")
                del pixmap
                key = relative.as_posix()
                checked = {"sha256": digest, "size_bytes": size}
                if key in reviewed and reviewed[key] != checked:
                    raise ValueError("核对原页重复记录的指纹不一致")
                reviewed[key] = checked
    except (OSError, KeyError, TypeError, AttributeError, ValueError, RuntimeError) as error:
        raise ValueError("核对原页图片无法安全读取，不能生成当前成绩") from error
    return dict(sorted(reviewed.items()))


def validate_revision(dataset, dataset_path):
    """只允许改处理快照；保留问题、分组、原书、条号和PDF文件页码。"""
    revision = dataset.get("binding_revision")
    if revision is None:
        return
    try:
        filename = revision["parent_file"]
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename):
            raise ValueError
        parent_path = Path(dataset_path).parent / filename
        if parent_path.resolve() == Path(dataset_path).resolve():
            raise ValueError
        raw = parent_path.read_bytes()
        parent = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != revision["parent_sha256"]
                or parent.get("binding_revision") is not None
                or parent["version"] == dataset["version"]
                or len(parent["cases"]) != len(dataset["cases"])):
            raise ValueError
        changed = {}
        for before, after in zip(parent["cases"], dataset["cases"], strict=True):
            normalized = copy.deepcopy(after)
            if len(before.get("expected", [])) != len(after.get("expected", [])):
                raise ValueError
            for index, (old, new) in enumerate(zip(before.get("expected", []),
                                                   after.get("expected", []), strict=True)):
                normalized["expected"][index]["snapshot_id"] = old["snapshot_id"]
                if old["snapshot_id"] != new["snapshot_id"]:
                    changed[(before["id"], index)] = (old, new)
            # 对比整道题，而不只比较query；边界行为、答案列表顺序也不能改变。
            if normalized != before:
                raise ValueError
        reviews = revision["reviews"]
        if not isinstance(reviews, list) or len(reviews) != len(changed) or not changed:
            raise ValueError
        seen = set()
        for review in reviews:
            key = (review["case_id"], review["target_index"])
            if (type(review["target_index"]) is not int or key in seen or key not in changed
                    or (review["before"], review["after"]) != changed[key]
                    or review["status"] != "DEVELOPER_PAGE_CHECKED"
                    or not isinstance(review["note"], str) or not review["note"].strip()
                    or not re.fullmatch(r"[a-f0-9]{64}", review["file_sha256"])):
                raise ValueError
            date.fromisoformat(review["checked_at"])
            target = review["after"]
            pages = review["pages"]
            expected_pages = list(range(target["page_start"], target["page_end"] + 1))
            if (not isinstance(pages, list) or [row["pdf_page"] for row in pages] != expected_pages
                    or not all(type(row["pdf_page"]) is int and
                               re.fullmatch(r"[a-f0-9]{64}", row["image_sha256"]) for row in pages)):
                raise ValueError
            seen.add(key)
    except (OSError, KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError("题库处理绑定修订未通过父版本与逐页核对，不能换题或自动替换答案") from error


def validate_originals(dataset, snapshots, documents):
    """两个处理结果必须绑定相同原件；同条款号不能证明版本或文件相同。"""
    available = {(row["source_id"], row["snapshot_id"]): row for row in snapshots}
    for review in dataset.get("binding_revision", {}).get("reviews", []):
        old, new = review["before"], review["after"]
        try:
            current = available[(new["source_id"], new["snapshot_id"])]
            path = documents.cache_dir / old["source_id"] / f"{old['snapshot_id']}.json"
            historical = json.loads(path.read_bytes())
            if (historical["source_id"] != old["source_id"]
                    or historical["snapshot_id"] != old["snapshot_id"]
                    or historical["sha256"] != current["sha256"]
                    or current["sha256"] != review["file_sha256"]
                    or historical["page_count"] != current["page_count"]):
                raise ValueError
            # 声明一致还不代表文件可用；核对实际新旧原件，不重新识别正文。
            documents._check_cached_file(historical, old["source_id"])
            documents._check_cached_file(current, new["source_id"])
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise ValueError("题库新旧处理快照的原件指纹不一致或无法核对，拒绝评分") from error
