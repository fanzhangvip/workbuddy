"""
进程环境变量集中设置。

!! 重要 !!
本模块的 setup_environment() 必须在**创建 QApplication 之前**调用，
最好在任何 QtMultimedia 相关对象实例化之前调用，否则设置不生效。
因此 main.py 的第一件事就是调用它，且要早于 `import PySide6.QtWidgets`。

为什么需要这些设置:
- Qt6 的 QtMultimedia 默认使用 FFmpeg 后端，但 FFmpeg 出于安全考虑
  **默认禁止"嵌套协议"**（即被其他协议内部使用的协议）。
  而 HLS(m3u8) 内部会嵌套 http/https + crypto(AES-128 密钥)，
  RTSP 内部会嵌套 rtp/rtcp/udp/tcp。
  不放行的话，m3u8 与 rtsp 会直接打开失败。
- Windows 原生 WMF 后端能力弱（Qt 6.10 起已废弃），强制走 FFmpeg 后端。
"""

from __future__ import annotations

import os

# 允许的"嵌套协议"白名单。逗号分隔，不能有空格。
_PROTOCOLS = (
    # 本地与内联
    "file",
    "crypto",
    "data",
    # 通用网络 / HLS 分片 / 加密传输
    "http",
    "https",
    "tcp",
    "tls",
    "httpproxy",
    # RTP/RTCP/UDP，RTSP 的承载协议
    "rtp",
    "rtcp",
    "udp",
    # 实时流
    "rtsp",
    "rtsps",
    # RTMP 家族
    "rtmp",
    "rtmpt",
    "rtmpe",
    "rtmps",
    "rtmpte",
)


def setup_environment(*, debug: bool = False) -> None:
    """设置 Qt / FFmpeg 运行时环境变量。幂等，可重复调用。"""
    # 1) 强制使用 FFmpeg 媒体后端（Windows 原生 WMF 后端能力弱且已废弃）
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

    # 2) 放行嵌套协议。这是 m3u8 / rtsp 能否播放的关键开关。
    #    注意: 只能"放行"已编译进 FFmpeg 的协议，无法凭空让缺失的协议出现。
    os.environ["QT_FFMPEG_PROTOCOL_WHITELIST"] = ",".join(_PROTOCOLS)

    # 3) 注意：这里曾经设过 QT_FFMPEG_RTSP_TRANSPORT=tcp，但它是**无效的**。
    #    实测（扫描 ffmpegmediaplugin.dll 与 Qt6Multimedia.dll 中全部
    #    QT_* 字符串）确认 PySide6 6.11.2 并不识别该变量：
    #      ffmpegmediaplugin.dll 里只有 QT_FFMPEG_PROTOCOL_WHITELIST、
    #      QT_FFMPEG_DEBUG、QT_FFMPEG_DECODING_HW_DEVICE_TYPES 等，
    #      没有 rtsp_transport / rtsp_flags 这类开关；
    #      avformat-61.dll 里 rtsp 协议别名也只有 rtsp 和 rtsps。
    #    也就是说：**Qt 的 FFmpeg 后端播放 RTSP 只能用 UDP，无法改为 TCP。**
    #    留着这个变量会让日志看起来"已经强制走 TCP 了"，属于误导，故删除。
    #    若某天 Qt 支持了该变量，再在这里加回来并更新本注释。

    # 4) 启用实验性编解码器（webm 里的 Opus/Vorbis 需要）
    os.environ.setdefault("QT_ENABLE_EXPERIMENTAL_CODECS", "1")

    # 5) 高 DPI 缩放
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    # 6) 排查用日志，默认关闭
    if debug or os.environ.get("VIDEODEMO_DEBUG") == "1":
        os.environ.setdefault("QT_FFMPEG_DEBUG", "1")
        os.environ.setdefault(
            "QT_LOGGING_RULES", "*.multimedia.*=true;*.ffmpeg.*=true"
        )


#: RTSP 传输方式在 Qt 后端中不可配置。集中放在这里，供提示文案引用，
#: 避免同一句结论散落在多处而逐渐失真。
RTSP_TRANSPORT_NOTE = (
    "内置内核播放 RTSP 时走 UDP 传输，且 Qt 未提供修改该行为的开关"
    "（无 rtsp_transport 选项、也无对应环境变量）。"
)


def describe_environment() -> str:
    """返回环境变量摘要，便于日志与排查。"""
    keys = (
        "QT_MEDIA_BACKEND",
        "QT_FFMPEG_PROTOCOL_WHITELIST",
        "QT_ENABLE_EXPERIMENTAL_CODECS",
        "QT_FFMPEG_DEBUG",
    )
    lines = [f"  {k} = {os.environ.get(k, '<unset>')}" for k in keys]
    lines.append(f"  提示 = {RTSP_TRANSPORT_NOTE}")
    return "\n".join(lines)
