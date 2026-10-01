# coding: utf-8
"""
服务端共享 PCM 分段核心。

本模块只负责有界 PCM 缓冲、切点吸附、偏移/重叠和段长预算；连接取消、
WebSocket 背压以及 HTTP 持久确认由各自 caller 负责。当前真实 caller 是
WebSocket 接收层，后续 HTTP runner 复用同一段输出契约。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from config_server import (
    Qwen3ASRGGUFArgs,
    QwenASRMLXArgs,
    ServerConfig as Config,
)
from core.constants import AudioFormat
from . import logger


def engine_segment_limit() -> float | None:
    """读取当前 ASR 配置的单段上限，不加载识别模型。"""
    model_type = Config.model_type.lower()
    if model_type == 'qwen_asr':
        return Qwen3ASRGGUFArgs.chunk_size
    if model_type == 'qwen_asr_mlx':
        return QwenASRMLXArgs.chunk_size
    return None


def validate_segment_params(nominal: float, overlap: float) -> None:
    """无状态校验分段参数取值范围与引擎单段上限预算。

    与连接无关，只依赖 Config/QwenArgs 的权威取值；WS 接收层与后续 HTTP
    runner 共用这一份规则。任务首帧锁定等连接级语义由各自 caller 负责。
    """
    if not math.isfinite(nominal) or nominal < 5:
        raise ValueError(f"seg_duration={nominal:g} 不在允许范围 [5, +∞)")
    if not math.isfinite(overlap) or overlap < 0 or overlap >= nominal / 2:
        raise ValueError(
            f"seg_overlap={overlap:g} 不在允许范围 [0, seg_duration/2={nominal / 2:g})"
        )

    limit = engine_segment_limit()
    if limit is not None:
        if Config.seg_cut_snap:
            max_cut = max(Config.seg_max_cut, nominal + Config.seg_search_after)
            max_segment = max_cut + overlap
            values = (
                f"seg_max_cut={Config.seg_max_cut:g}, seg_duration={nominal:g}, "
                f"seg_search_after={Config.seg_search_after:g}, seg_overlap={overlap:g}"
            )
        else:
            max_segment = nominal + overlap
            values = f"seg_duration={nominal:g}, seg_overlap={overlap:g}"
        if max_segment > limit:
            raise ValueError(
                f"{values} 导致单段最长 {max_segment:g}s，允许范围 ≤ 引擎上限 {limit:g}s"
            )


@dataclass(frozen=True)
class PcmSegment:
    """从有界 PCM 缓冲取出的一个待提交片段。"""

    data: bytes
    offset: float
    overlap: float
    is_final: bool


class PcmSegmenter:
    """复用 WS/HTTP 的 PCM 分段状态与提交前段预算。"""

    def __init__(self):
        self.chunks: bytes = b''
        self.offset: float = 0.0
        self.byte_count: int = 0
        self.search_to: float = 0.0
        self.cut_finder = None
        self.engine_segment_limit: float | None = None
        self.cut_snap = False
        self.search_before = 0.0
        self.search_after = 0.0
        self.max_cut = 0.0

    @property
    def duration(self) -> float:
        """当前有界 PCM 缓冲时长。"""
        return AudioFormat.bytes_to_seconds(len(self.chunks))

    @property
    def total_duration(self) -> float:
        """累计接收 PCM 时长。"""
        return AudioFormat.bytes_to_seconds(self.byte_count)

    def configure(
        self,
        *,
        cut_finder,
        engine_segment_limit: float | None,
        cut_snap: bool,
        search_before: float,
        search_after: float,
        max_cut: float,
    ) -> None:
        """绑定当前服务端分段策略；不复制连接或持久化状态。"""
        self.cut_finder = cut_finder
        self.engine_segment_limit = engine_segment_limit
        self.cut_snap = cut_snap
        self.search_before = search_before
        self.search_after = search_after
        self.max_cut = max_cut

    def append(self, data: bytes) -> None:
        """追加 PCM 并保留累计字节数。"""
        self.chunks += data
        self.byte_count += len(data)

    def reset(self) -> None:
        """清空当前任务的 PCM 与切点状态。"""
        self.chunks = b''
        self.offset = 0.0
        self.byte_count = 0
        self.search_to = 0.0

    async def drain_ready(
        self,
        *,
        source: str,
        nominal: float,
        overlap: float,
        is_final: bool = False,
    ) -> list[PcmSegment]:
        """取出当前可提交的非末段，末帧时保留一个最终段预算。"""
        if self.cut_snap:
            return await self._drain_with_cut_snap(
                source=source,
                nominal=nominal,
                overlap=overlap,
                is_final=is_final,
            )
        return self._drain_fixed(
            nominal=nominal,
            overlap=overlap,
            is_final=is_final,
        )

    def final_segment(self, overlap: float) -> PcmSegment:
        """返回剩余 PCM 作为唯一最终段，不伪造额外片段。"""
        segment = PcmSegment(
            data=self.chunks,
            offset=self.offset,
            overlap=overlap,
            is_final=True,
        )
        self._assert_within_limit(segment.data)
        return segment

    def _drain_fixed(
        self,
        *,
        nominal: float,
        overlap: float,
        is_final: bool,
    ) -> list[PcmSegment]:
        limit = self.engine_segment_limit
        final_limit = limit if limit is not None else nominal + overlap
        segments = []
        while (
            self.duration > final_limit
            if is_final
            else self.duration >= nominal + overlap * 2
        ):
            segments.append(self._cut(nominal, overlap))
        return segments

    async def _drain_with_cut_snap(
        self,
        *,
        source: str,
        nominal: float,
        overlap: float,
        is_final: bool,
    ) -> list[PcmSegment]:
        if self.cut_finder is None:
            raise RuntimeError('启用切点吸附时必须提供 CutFinder')
        w_before, w_after = self.search_before, self.search_after
        max_cut = max(self.max_cut, nominal + w_after)
        lo = max(nominal - w_before, min(nominal, 1.0))
        limit = self.engine_segment_limit
        final_limit = limit if limit is not None else max_cut + overlap
        loop = asyncio.get_running_loop()
        segments = []

        while True:
            if is_final:
                if self.duration <= final_limit:
                    return segments
            elif self.duration < nominal + w_after + overlap:
                return segments

            if source == 'file':
                hi = min(self.duration - overlap, max_cut)
                if not is_final and hi < max_cut and hi <= self.search_to:
                    return segments
            else:
                hi = nominal + w_after

            cut, confident = await loop.run_in_executor(
                None,
                self.cut_finder.find,
                self.chunks,
                lo,
                hi,
                nominal,
            )
            if (
                not is_final
                and not confident
                and source == 'file'
                and hi < max_cut
            ):
                self.search_to = hi
                logger.debug(
                    f"切点吸附: [{lo:.1f}, {hi:.1f}]s 无可信断点，等待更多音频延长搜索"
                )
                return segments
            if confident:
                logger.debug(
                    f"切点吸附: 名义 {nominal}s，在 {cut:.2f}s 找到静音断点"
                )
            else:
                logger.info(
                    f"切点吸附: [{lo:.1f}, {hi:.1f}]s 内无可信静音断点，"
                    f"取最低分点 {cut:.2f}s 下刀"
                )
            self.search_to = 0.0
            segments.append(self._cut(cut, overlap))

    def _cut(self, cut: float, overlap: float) -> PcmSegment:
        stride_samples = round(cut * AudioFormat.SAMPLE_RATE)
        overlap_samples = round(overlap * AudioFormat.SAMPLE_RATE)
        stride_bytes = stride_samples * AudioFormat.BYTES_PER_SAMPLE
        segment_bytes = (stride_samples + overlap_samples) * AudioFormat.BYTES_PER_SAMPLE
        segment_data = self.chunks[:segment_bytes]
        self._assert_within_limit(segment_data)
        segment = PcmSegment(
            data=segment_data,
            offset=self.offset,
            overlap=overlap,
            is_final=False,
        )
        self.chunks = self.chunks[stride_bytes:]
        self.offset += stride_bytes / AudioFormat.BYTES_PER_SECOND
        return segment

    def _assert_within_limit(self, data: bytes) -> None:
        if (
            self.engine_segment_limit is not None
            and AudioFormat.bytes_to_seconds(len(data))
            > self.engine_segment_limit
        ):
            raise AssertionError(
                f"提交段长 {AudioFormat.bytes_to_seconds(len(data)):.6f}s "
                f"超过引擎单段上限 {self.engine_segment_limit:.6f}s"
            )
