from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Protocol


class CapabilityUnavailable(RuntimeError):
    pass


class DatasetAdapter(Protocol):
    kind: str

    def inspect(self, files: list[Path]) -> dict[str, Any]: ...
    def read_metadata(self, files: list[Path]) -> dict[str, str]: ...
    def list_channels(self, files: list[Path]) -> list[dict[str, Any]]: ...
    def read_channel(self, files: list[Path], channel: str) -> dict[str, Any]: ...
    def read_window(self, files: list[Path], channel: str, start: float, end: float) -> dict[str, Any]: ...
    def compare_versions(self, files: list[Path]) -> dict[str, Any]: ...
    def query_value(self, files: list[Path], name: str) -> dict[str, Any]: ...


def _require_asammdf():
    try:
        from asammdf import MDF
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise CapabilityUnavailable("未安装 asammdf，无法解析 MDF/MF4/DAT 信号；请先安装依赖") from exc
    return MDF


def _filename_date(name: str) -> str:
    """Pull a leading YYYY-MM-DD out of the filename; '' if absent.

    Handles the stored sha prefix and date separators -, _, . or none.
    """
    stem = re.sub(r"^[0-9a-f]{64}-", "", Path(name).stem)
    match = re.match(r"\s*((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", stem)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""


def extract_mdf_metadata(path: Path, source_name: str | None = None) -> dict[str, str]:
    """Deterministic editable fields: date (filename or header) plus the experiment
    and A2L/HEX names from the MDF header. Order matches models.ANNOTATION_FIELDS."""
    result = {"measurement_date": _filename_date(source_name or path.name), "a2l": "", "hex": "", "experiment": ""}
    try:
        MDF = _require_asammdf()
        with MDF(path, channels=[]) as mdf:
            comment = str(mdf.header.comment or "")
            experiment = re.search(r"Experiment:\s*([^\r\n<]+)", comment)
            if experiment:
                result["experiment"] = experiment.group(1).strip()
            if not result["measurement_date"]:
                start_time = getattr(mdf, "start_time", None)
                if isinstance(start_time, datetime):
                    result["measurement_date"] = start_time.date().isoformat()
            names = re.findall(r"[^\s<>\"']+\.(?:a2l|hex)\b", comment, flags=re.IGNORECASE)
            names += [str(getattr(item, "file_name", "") or "") for item in getattr(mdf, "attachments", [])]
            result["a2l"] = " / ".join(dict.fromkeys(Path(n).name for n in names if n.lower().endswith(".a2l")))
            result["hex"] = " / ".join(dict.fromkeys(Path(n).name for n in names if n.lower().endswith(".hex")))
    except Exception:
        # ponytail: return filename metadata when a recorder produced an incomplete MDF.
        pass
    return result


@dataclass
class MDFAdapter:
    kind: str = "mdf"

    # Kept lightweight on purpose: this runs on every /api/datasets list, so it
    # must not open and parse the files. Real channel scanning is on demand.
    def inspect(self, files: list[Path]) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "files": [path.name for path in files],
            "capabilities": {"inspect": True, "metadata": True, "list_channels": True, "read_channel": True, "read_window": True},
            "note": "MDF 信号解析由 asammdf 提供（按需读取，不在列表时解析）。",
        }

    def read_metadata(self, files: list[Path]) -> dict[str, str]:
        if not files:
            raise ValueError("数据集中没有 MDF 文件")
        return extract_mdf_metadata(files[0])

    def list_channels(self, files: list[Path]) -> list[dict[str, Any]]:
        MDF = _require_asammdf()
        seen: dict[str, dict[str, Any]] = {}
        for path in files:
            with MDF(path) as mdf:
                for name, occurrences in mdf.channels_db.items():
                    if not name or name in ("time", "t"):
                        continue
                    if name not in seen:
                        group_index, channel_index = occurrences[0]
                        unit = mdf.groups[group_index].channels[channel_index].unit or ""
                        seen[name] = {"name": name, "unit": unit, "files": [path.name]}
                    elif path.name not in seen[name]["files"]:
                        seen[name]["files"].append(path.name)
        return sorted(seen.values(), key=lambda channel: channel["name"])

    def read_channel(self, files: list[Path], channel: str) -> dict[str, Any]:
        MDF = _require_asammdf()
        segments: list[dict[str, Any]] = []
        unit = ""
        for path in files:
            with MDF(path) as mdf:
                if channel not in mdf.channels_db:
                    continue
                signal = mdf.get(channel)
                unit = unit or (signal.unit or "")
                segments.append({
                    "file": path.name,
                    "timestamps": signal.timestamps.tolist(),
                    "samples": signal.samples.tolist(),
                })
        if not segments:
            raise ValueError(f"数据集中未找到信号: {channel}")
        return {"channel": channel, "unit": unit, "segments": segments}

    def read_window(self, files: list[Path], channel: str, start: float, end: float) -> dict[str, Any]:
        if start > end:
            raise ValueError("窗口起点不能晚于终点")
        data = self.read_channel(files, channel)
        out: list[dict[str, Any]] = []
        for segment in data["segments"]:
            points = [
                {"t": t, "v": v}
                for t, v in zip(segment["timestamps"], segment["samples"])
                if start <= t <= end
            ]
            out.append({"file": segment["file"], "points": points})
        return {"channel": channel, "unit": data["unit"], "start": start, "end": end, "segments": out}

    def compare_versions(self, files: list[Path]) -> dict[str, Any]:
        raise CapabilityUnavailable("MDF 版本语义比较不在首版范围内")

    def query_value(self, files: list[Path], name: str) -> dict[str, Any]:
        raise CapabilityUnavailable("MDF 数据请使用 read_window 工具")


@dataclass
class CalibrationAdapter:
    kind: str = "calibration"

    def inspect(self, files: list[Path]) -> dict[str, Any]:
        a2l = [path.name for path in files if path.suffix.lower() == ".a2l"]
        hex_files = [path.name for path in files if path.suffix.lower() == ".hex"]
        return {
            "kind": self.kind,
            "a2l": a2l,
            "hex": hex_files,
            "capabilities": {"inspect": True, "compare_versions": True, "query_value": False},
            "note": "首版比较文件身份与版本关系；标定量语义查询由后续解析插件提供。",
        }

    def read_metadata(self, files: list[Path]) -> dict[str, str]:
        raise CapabilityUnavailable("标定数据不提供 MDF 属性")

    def list_channels(self, files: list[Path]) -> list[dict[str, Any]]:
        raise CapabilityUnavailable("标定数据不提供测量通道")

    def read_channel(self, files: list[Path], channel: str) -> dict[str, Any]:
        raise CapabilityUnavailable("标定数据不是时间序列")

    def read_window(self, files: list[Path], channel: str, start: float, end: float) -> dict[str, Any]:
        raise CapabilityUnavailable("标定数据不是时间序列")

    def compare_versions(self, files: list[Path]) -> dict[str, Any]:
        from backend.services.calibration import compare_files

        a2l_files = [path for path in files if path.suffix.lower() == ".a2l"]
        hex_files = [path for path in files if path.suffix.lower() == ".hex"]
        changes: list[dict[str, Any]] = []
        semantic_available = False
        if len(a2l_files) == 1 and len(hex_files) == 2:
            try:
                semantic_available, changes = compare_files(a2l_files[0], hex_files[0], hex_files[1])
            except (OSError, UnicodeError, ValueError):
                semantic_available = False
        return {
            "versions": [{"name": path.name, "size": path.stat().st_size} for path in hex_files],
            "same_size": len({path.stat().st_size for path in hex_files}) <= 1 if hex_files else True,
            "semantic_diff_available": semantic_available,
            "changes": changes,
        }

    def query_value(self, files: list[Path], name: str) -> dict[str, Any]:
        raise CapabilityUnavailable("当前 A2L/HEX 适配器尚未注册语义解析后端")


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, DatasetAdapter] = {}

    def register(self, adapter: DatasetAdapter) -> None:
        self._adapters[adapter.kind] = adapter

    def get(self, kind: str) -> DatasetAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            raise ValueError(f"未知数据集类型: {kind}") from exc

    def capabilities(self) -> dict[str, dict[str, Any]]:
        return {name: adapter.inspect([]) for name, adapter in self._adapters.items()}


registry = AdapterRegistry()
registry.register(MDFAdapter())
registry.register(CalibrationAdapter())
