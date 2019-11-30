import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, TypeVar

from pytivo.config import (
    get169Blacklist,
    get169Letterbox,
    get169Setting,
    getAudioBR,
    getBuffSize,
    getFFmpegPrams,
    getMaxAudioBR,
    getMaxVideoBR,
    getOptres,
    getTivoHeight,
    getTivoWidth,
    getVideoBR,
    get_bin,
    get_server,
    get_tsn,
    isHDtivo,
    nearestTivoHeight,
    nearestTivoWidth,
    strtod,
)
from pytivo.metadata import video_info, VideoInfo

LOGGER = logging.getLogger(__name__)


class FfmpegProcess:
    def __init__(
        self,
        process: subprocess.Popen,
        start: int,
        end: int,
        last_read: float,
        blocks: List[bytes],
    ):
        self.process = process
        self.start = start
        self.end = end
        self.last_read = last_read
        self.blocks = blocks


FFMPEG_PROCS: Dict[str, FfmpegProcess] = {}
REAPERS: Dict[str, Any] = {}

GOOD_MPEG_FPS = ["23.98", "24.00", "25.00", "29.97", "30.00", "50.00", "59.94", "60.00"]

BLOCKSIZE = 512 * 1024
MAXBLOCKS = 2
TIMEOUT = 600

T = TypeVar("T")


def transcode_settings(
    isQuery: bool, inFile: str, tsn: str = "", mime: str = ""
) -> List[str]:
    vcodec = select_videocodec(inFile, tsn, mime)

    settings = select_buffsize(tsn) + vcodec
    if not vcodec[1] == "copy":
        settings += (
            select_videobr(inFile, tsn)
            + select_maxvideobr(tsn)
            + select_videofps(inFile, tsn)
            + select_aspect(inFile, tsn)
        )

    acodec = select_audiocodec(isQuery, inFile, tsn)
    settings += acodec
    if not acodec[1] == "copy":
        settings += (
            select_audiobr(tsn)
            + select_audiofr(inFile, tsn)
            + select_audioch(inFile, tsn)
        )

    settings += [select_audiolang(inFile, tsn), select_ffmpegprams(tsn)]

    settings += select_format(tsn, mime)

    settings = " ".join(settings).split()
    return settings


def transcode(
    inFile: str, outFile: BinaryIO, tsn: str = "", mime: str = "", thead: bytes = b""
) -> int:
    settings = transcode_settings(isQuery=False, inFile=inFile, tsn=tsn, mime=mime)

    ffmpeg_path = get_bin("ffmpeg")
    if ffmpeg_path is None:
        LOGGER.error("No ffmpeg binary found")
        return 0

    if inFile[-5:].lower() == ".tivo":
        tivodecode_path = get_bin("tivodecode")
        if tivodecode_path is None:
            LOGGER.error("No tivodecode binary found.")
            return 0
        tivo_mak = get_server("tivo_mak", "")
        if tivo_mak == "":
            LOGGER.error("No valid tivo_mak found.")
            return 0
        tcmd = [tivodecode_path, "-m", tivo_mak, inFile]
        tivodecode = subprocess.Popen(
            tcmd, stdout=subprocess.PIPE, bufsize=(512 * 1024)
        )
        if tivo_compatible(inFile, tsn)[0]:
            cmd = [""]
            ffmpeg = tivodecode
        else:
            cmd = [ffmpeg_path, "-i", "-"] + settings
            ffmpeg = subprocess.Popen(
                cmd,
                stdin=tivodecode.stdout,
                stdout=subprocess.PIPE,
                bufsize=(512 * 1024),
            )
    else:
        cmd = [ffmpeg_path, "-i", inFile] + settings
        ffmpeg = subprocess.Popen(cmd, bufsize=(512 * 1024), stdout=subprocess.PIPE)

    if cmd:
        LOGGER.debug("transcoding to tivo model " + tsn[:3] + " using ffmpeg command:")
        LOGGER.debug(" ".join(cmd))

    FFMPEG_PROCS[inFile] = FfmpegProcess(
        process=ffmpeg, start=0, end=0, last_read=time.time(), blocks=[]
    )
    if thead:
        FFMPEG_PROCS[inFile].blocks.append(thead)
    reap_process(inFile)
    return resume_transfer(inFile, outFile, 0)


def is_resumable(inFile: str, offset: int) -> bool:
    if inFile in FFMPEG_PROCS:
        proc = FFMPEG_PROCS[inFile]
        if proc.start <= offset < proc.end:
            return True
        else:
            cleanup(inFile)
            kill(proc.process)
    return False


def resume_transfer(inFile: str, outFile: BinaryIO, offset: int) -> int:
    proc = FFMPEG_PROCS[inFile]
    offset -= proc.start
    count = 0

    try:
        for block in proc.blocks:
            length = len(block)
            if offset < length:
                if offset > 0:
                    block = block[offset:]
                outFile.write(b"%x\r\n" % len(block))
                outFile.write(block)
                outFile.write(b"\r\n")
                count += len(block)
            offset -= length
        outFile.flush()
    except Exception as msg:
        LOGGER.info(msg)
        return count

    proc.start = proc.end
    proc.blocks = []

    return count + transfer_blocks(inFile, outFile)


def transfer_blocks(inFile: str, outFile: BinaryIO) -> int:
    proc = FFMPEG_PROCS[inFile]
    blocks = proc.blocks
    count = 0

    while True:
        try:
            block = proc.process.stdout.read(BLOCKSIZE)
            proc.last_read = time.time()
        except Exception as msg:
            LOGGER.info(msg)
            cleanup(inFile)
            kill(proc.process)
            break

        if not block:
            try:
                outFile.flush()
            except Exception as msg:
                LOGGER.info(msg)
            else:
                cleanup(inFile)
            break

        blocks.append(block)
        proc.end += len(block)
        if len(blocks) > MAXBLOCKS:
            proc.start += len(blocks[0])
            blocks.pop(0)

        try:
            outFile.write(b"%x\r\n" % len(block))
            outFile.write(block)
            outFile.write(b"\r\n")
            count += len(block)
        except Exception as msg:
            LOGGER.info(msg)
            break

    return count


def reap_process(inFile: str) -> None:
    if FFMPEG_PROCS and inFile in FFMPEG_PROCS:
        proc = FFMPEG_PROCS[inFile]
        if proc.last_read + TIMEOUT < time.time():
            del FFMPEG_PROCS[inFile]
            del REAPERS[inFile]
            kill(proc.process)
        else:
            reaper = threading.Timer(TIMEOUT, reap_process, (inFile,))
            REAPERS[inFile] = reaper
            reaper.start()


def cleanup(inFile: str) -> None:
    del FFMPEG_PROCS[inFile]
    REAPERS[inFile].cancel()
    del REAPERS[inFile]


def select_audiocodec(
    isQuery: bool, inFile: str, tsn: str = "", mime: str = ""
) -> List[str]:
    if inFile[-5:].lower() == ".tivo":
        return ["-c:a", "copy"]
    vInfo = video_info(inFile)
    codectype = vInfo.vCodec
    # Default, compatible with all TiVo's
    codec = "ac3"
    compatiblecodecs = ("ac3", "liba52", "mp2")

    if vInfo.aCodec in compatiblecodecs:
        aKbps = vInfo.aKbps
        aCh = vInfo.aCh
        if aKbps is None:
            if not isQuery:
                vInfoQuery = audio_check(inFile, tsn)
                if vInfoQuery is None:
                    aKbps = None
                    aCh = None
                else:
                    aKbps = vInfoQuery.aKbps
                    aCh = vInfoQuery.aCh
            else:
                codec = "TBA"
        if aKbps and aKbps <= getMaxAudioBR(tsn):
            # compatible codec and bitrate, do not reencode audio
            codec = "copy"
        if vInfo.aCodec != "ac3" and (aCh is None or aCh > 2):
            codec = "ac3"
    val = ["-c:a", codec]
    if not (codec == "copy" and codectype == "mpeg2video"):
        val.append("-copyts")
    return val


def select_audiofr(inFile: str, tsn: str) -> List[str]:
    freq = "48000"  # default
    vInfo = video_info(inFile)
    if vInfo.aFreq == "44100":
        # compatible frequency
        freq = vInfo.aFreq
    return ["-ar", freq]


def select_audioch(inFile: str, tsn: str) -> List[str]:
    # AC-3 max channels is 5.1
    vInfo = video_info(inFile)
    if vInfo.aCh is not None and vInfo.aCh > 6:
        LOGGER.debug("Too many audio channels for AC-3, using 5.1 instead")
        return ["-ac", "6"]
    return []


def select_audiolang(inFile: str, tsn: str) -> str:
    vInfo = video_info(inFile)
    audio_lang = get_tsn("audio_lang", tsn)
    LOGGER.debug("audio_lang: %s" % audio_lang)
    if vInfo.mapAudio:
        # default to first detected audio stream to begin with
        stream = vInfo.mapAudio[0][0]
        LOGGER.debug("set first detected audio stream by default: %s" % stream)
    # TODO: why do we check mapVideo in the following?
    if (
        audio_lang is not None
        and vInfo.mapAudio is not None
        and vInfo.mapVideo is not None
    ):
        langmatch_curr = []
        langmatch_prev = vInfo.mapAudio[:]
        for lang in audio_lang.replace(" ", "").lower().split(","):
            LOGGER.debug("matching lang: %s" % lang)
            for s, l in langmatch_prev:
                if lang in s + l.replace(" ", "").lower():
                    LOGGER.debug("matched: %s" % s + l.replace(" ", "").lower())
                    langmatch_curr.append((s, l))
            # if only 1 item matched we're done
            if len(langmatch_curr) == 1:
                stream = langmatch_curr[0][0]
                LOGGER.debug("found exactly one match: %s" % stream)
                break
            # if more than 1 item matched copy the curr area to the prev
            # array we only need to look at the new shorter list from
            # now on
            elif len(langmatch_curr) > 1:
                langmatch_prev = langmatch_curr[:]
                # default to the first item matched thus far
                stream = langmatch_curr[0][0]
                LOGGER.debug("remember first match: %s" % stream)
                langmatch_curr = []
    # don't let FFmpeg auto select audio stream, pyTivo defaults to
    # first detected
    if stream and vInfo.mapVideo is not None:
        LOGGER.debug("selected audio stream: %s" % stream)
        return "-map " + vInfo.mapVideo + " -map " + stream
    # if no audio is found
    LOGGER.debug("selected audio stream: None detected")
    return ""


def select_videofps(inFile: str, tsn: str) -> List[str]:
    vInfo = video_info(inFile)
    fps = ["-r", "29.97"]  # default
    if isHDtivo(tsn) and vInfo.vFps in GOOD_MPEG_FPS:
        fps = []
    return fps


def select_videocodec(inFile: str, tsn: str, mime: str = "") -> List[str]:
    codec = ["-c:v"]
    vInfo = video_info(inFile)
    if tivo_compatible_video(vInfo, tsn, mime)[0]:
        codec.append("copy")
        if mime == "video/x-tivo-mpeg-ts":
            org_codec = vInfo.vCodec
            if org_codec == "h264":
                codec += ["-bsf:v", "h264_mp4toannexb", "-muxdelay", "0"]
            elif org_codec == "hevc":
                codec += ["-bsf:v", "hevc_mp4toannexb"]
    else:
        codec += ["mpeg2video", "-pix_fmt", "yuv420p"]  # default
    return codec


def select_videobr(inFile: str, tsn: str, mime: str = "") -> List[str]:
    return ["-b:v", str(select_videostr(inFile, tsn, mime) / 1000) + "k"]


def select_videostr(inFile: str, tsn: str, mime: str = "") -> int:
    vInfo = video_info(inFile)
    if tivo_compatible_video(vInfo, tsn, mime)[0] and vInfo.kbps is not None:
        video_str = vInfo.kbps
        if vInfo.aKbps is not None:
            video_str -= vInfo.aKbps
        video_str *= 1000
    else:
        video_str = strtod(getVideoBR(tsn))
        if isHDtivo(tsn) and vInfo.kbps:
            video_str = max(video_str, vInfo.kbps * 1000)
        video_str = int(min(strtod(getMaxVideoBR(tsn)) * 0.95, video_str))
    return video_str


def select_audiobr(tsn: str) -> List[str]:
    return ["-b:a", getAudioBR(tsn)]


def select_maxvideobr(tsn: str) -> List[str]:
    return ["-maxrate", getMaxVideoBR(tsn)]


def select_buffsize(tsn: str) -> List[str]:
    return ["-bufsize", getBuffSize(tsn)]


def select_ffmpegprams(tsn: str) -> str:
    params = getFFmpegPrams(tsn)
    if not params:
        params = ""
    return params


def select_format(tsn: str, mime: str) -> List[str]:
    if mime == "video/x-tivo-mpeg-ts":
        fmt = "mpegts"
    else:
        fmt = "vob"
    return ["-f", fmt, "-"]


def pad_TB(
    tivo_width: int, tivo_height: int, multiplier: float, vInfo: VideoInfo
) -> List[str]:
    if vInfo.vHeight is None or vInfo.vWidth is None:
        LOGGER.error("Internal Error: vInfo.vHeight is None or vInfo.vHeight is None.")
        return ["-vf", "scale=0:0,pad=0:0:0:0"]

    endHeight = int(((tivo_width * vInfo.vHeight) / vInfo.vWidth) * multiplier)
    if endHeight % 2:
        endHeight -= 1
    topPadding = (tivo_height - endHeight) / 2
    if topPadding % 2:
        topPadding -= 1
    return [
        "-vf",
        "scale=%d:%d,pad=%d:%d:0:%d"
        % (tivo_width, endHeight, tivo_width, tivo_height, topPadding),
    ]


def pad_LR(
    tivo_width: int, tivo_height: int, multiplier: float, vInfo: VideoInfo
) -> List[str]:
    if vInfo.vHeight is None or vInfo.vWidth is None:
        LOGGER.error("Internal Error: vInfo.vHeight is None or vInfo.vHeight is None.")
        return ["-vf", "scale=0:0,pad=0:0:0:0"]

    endWidth = int((tivo_height * vInfo.vWidth) / (vInfo.vHeight * multiplier))
    if endWidth % 2:
        endWidth -= 1
    leftPadding = (tivo_width - endWidth) / 2
    if leftPadding % 2:
        leftPadding -= 1
    return [
        "-vf",
        "scale=%d:%d,pad=%d:%d:%d:0"
        % (endWidth, tivo_height, tivo_width, tivo_height, leftPadding),
    ]


def select_aspect(inFile: str, tsn: str = "") -> List[str]:
    tivo_width = getTivoWidth(tsn)
    tivo_height = getTivoHeight(tsn)

    vInfo = video_info(inFile)

    LOGGER.debug("tsn: %s" % tsn)

    aspect169 = get169Setting(tsn)

    LOGGER.debug("aspect169: %s" % aspect169)

    optres = getOptres(tsn)

    LOGGER.debug("optres: %s" % optres)

    if vInfo.vHeight is None or vInfo.vWidth is None:
        LOGGER.error("Internal Error: vInfo.vHeight is None or vInfo.vHeight is None.")
        return []

    if optres:
        optHeight = nearestTivoHeight(vInfo.vHeight)
        optWidth = nearestTivoWidth(vInfo.vWidth)
        if optHeight < tivo_height:
            tivo_height = optHeight
        if optWidth < tivo_width:
            tivo_width = optWidth

    if vInfo.par2:
        par2 = vInfo.par2
    elif vInfo.par:
        par2 = float(vInfo.par)
    else:
        # Assume PAR = 1.0
        par2 = 1.0

    LOGGER.debug(
        (
            "File=%s vCodec=%s vWidth=%s vHeight=%s vFps=%s millisecs=%s "
            + "tivo_height=%s tivo_width=%s"
        )
        % (
            inFile,
            vInfo.vCodec,
            vInfo.vWidth,
            vInfo.vHeight,
            vInfo.vFps,
            vInfo.millisecs,
            tivo_height,
            tivo_width,
        )
    )

    if isHDtivo(tsn) and not optres:
        if vInfo.par:
            npar = par2

            # adjust for pixel aspect ratio, if set

            if npar < 1.0:
                return ["-s", "%dx%d" % (vInfo.vWidth, math.ceil(vInfo.vHeight / npar))]
            elif npar > 1.0:
                # FFMPEG expects width to be a multiple of two
                return [
                    "-s",
                    "%dx%d" % (math.ceil(vInfo.vWidth * npar / 2.0) * 2, vInfo.vHeight),
                ]

        if vInfo.vHeight <= tivo_height:
            # pass all resolutions to S3, except heights greater than
            # conf height
            return []
        # else, resize video.

    d = gcd(vInfo.vHeight, vInfo.vWidth)
    rheight, rwidth = vInfo.vHeight / d, vInfo.vWidth / d
    LOGGER.debug("rheight=%s rwidth=%s" % (rheight, rwidth))

    if (rwidth, rheight) in [(1, 1)] and vInfo.par1 == "8:9":
        LOGGER.debug("File + PAR is within 4:3.")
        return ["-aspect", "4:3", "-s", "%sx%s" % (tivo_width, tivo_height)]

    elif (rwidth, rheight) in [
        (4, 3),
        (10, 11),
        (15, 11),
        (59, 54),
        (59, 72),
        (59, 36),
        (59, 54),
    ] or vInfo.dar1 == "4:3":
        LOGGER.debug("File is within 4:3 list.")
        return ["-aspect", "4:3", "-s", "%sx%s" % (tivo_width, tivo_height)]

    elif (
        (rwidth, rheight) in [(16, 9), (20, 11), (40, 33), (118, 81), (59, 27)]
        or vInfo.dar1 == "16:9"
    ) and (aspect169 or get169Letterbox(tsn)):
        LOGGER.debug("File is within 16:9 list and 16:9 allowed.")

        if get169Blacklist(tsn) or (aspect169 and get169Letterbox(tsn)):
            aspect = "4:3"
        else:
            aspect = "16:9"
        return ["-aspect", aspect, "-s", "%sx%s" % (tivo_width, tivo_height)]

    else:
        settings = ["-aspect"]

        multiplier16by9 = (16.0 * tivo_height) / (9.0 * tivo_width) / par2
        multiplier4by3 = (4.0 * tivo_height) / (3.0 * tivo_width) / par2
        ratio = vInfo.vWidth * 100 * par2 / vInfo.vHeight
        LOGGER.debug(
            "par2=%.3f ratio=%.3f mult4by3=%.3f" % (par2, ratio, multiplier4by3)
        )

        # If video is wider than 4:3 add top and bottom padding

        if ratio > 133:  # Might be 16:9 file, or just need padding on
            # top and bottom

            if aspect169 and ratio > 135:  # If file would fall in 4:3
                # assume it is supposed to be 4:3

                if get169Blacklist(tsn) or get169Letterbox(tsn):
                    settings.append("4:3")
                else:
                    settings.append("16:9")

                if ratio > 177:  # too short needs padding top and bottom
                    settings += pad_TB(tivo_width, tivo_height, multiplier16by9, vInfo)
                    LOGGER.debug(
                        (
                            "16:9 aspect allowed, file is wider "
                            + "than 16:9 padding top and bottom\n%s"
                        )
                        % " ".join(settings)
                    )

                else:  # too skinny needs padding on left and right.
                    settings += pad_LR(tivo_width, tivo_height, multiplier16by9, vInfo)
                    LOGGER.debug(
                        (
                            "16:9 aspect allowed, file is narrower "
                            + "than 16:9 padding left and right\n%s"
                        )
                        % " ".join(settings)
                    )

            else:  # this is a 4:3 file or 16:9 output not allowed
                if ratio > 135 and get169Letterbox(tsn):
                    settings.append("16:9")
                    multiplier = multiplier16by9
                else:
                    settings.append("4:3")
                    multiplier = multiplier4by3
                settings += pad_TB(tivo_width, tivo_height, multiplier, vInfo)
                LOGGER.debug(
                    ("File is wider than 4:3 padding " + "top and bottom\n%s")
                    % " ".join(settings)
                )

        # If video is taller than 4:3 add left and right padding, this
        # is rare. All of these files will always be sent in an aspect
        # ratio of 4:3 since they are so narrow.

        else:
            settings.append("4:3")
            settings += pad_LR(tivo_width, tivo_height, multiplier4by3, vInfo)
            LOGGER.debug(
                "File is taller than 4:3 padding left and right\n%s"
                % " ".join(settings)
            )

        return settings


def tivo_compatible_video(
    vInfo: VideoInfo, tsn: str, mime: str = ""
) -> Tuple[bool, str]:
    message = (True, "")
    codec = vInfo.vCodec
    if mime == "video/x-tivo-mpeg-ts":
        if not (codec in ("h264", "mpeg2video")):
            message = (False, "vCodec %s not compatible" % codec)
        return message

    if codec not in ("mpeg2video", "mpeg1video"):
        return (False, "vCodec %s not compatible" % codec)

    if vInfo.kbps is not None and vInfo.aKbps is not None:
        if vInfo.kbps - max(0, vInfo.aKbps) > strtod(getMaxVideoBR(tsn)) / 1000:
            return (False, "%s kbps exceeds max video bitrate" % vInfo.kbps)
    else:
        return (False, "%s kbps not supported" % vInfo.kbps)

    if isHDtivo(tsn):
        # HD Tivo detected, skipping remaining tests.
        return message

    if vInfo.vFps not in ["29.97", "59.94"]:
        return (False, "%s vFps, should be 29.97" % vInfo.vFps)

    if (get169Blacklist(tsn) and not get169Setting(tsn)) or (
        get169Letterbox(tsn) and get169Setting(tsn)
    ):
        if vInfo.dar1 and vInfo.dar1 not in ("4:3", "8:9", "880:657"):
            return (
                False,
                ("DAR %s not supported " + "by BLACKLIST_169 tivos") % vInfo.dar1,
            )

    mode = (vInfo.vWidth, vInfo.vHeight)
    if mode not in [
        (720, 480),
        (704, 480),
        (544, 480),
        (528, 480),
        (480, 480),
        (352, 480),
        (352, 240),
    ]:
        message = (False, "%s x %s not in supported modes" % mode)

    return message


def tivo_compatible_audio(
    vInfo: VideoInfo, inFile: str, tsn: str, mime: str = ""
) -> Tuple[bool, str]:
    message = (True, "")
    while True:
        codec = vInfo.aCodec

        if codec is None:
            LOGGER.debug("No audio stream detected")
            break

        if inFile[-5:].lower() == ".tivo":
            break

        if mime == "video/x-tivo-mpeg-ts":
            if codec not in ("ac3", "liba52", "mp2", "aac_latm"):
                message = (False, "aCodec %s not compatible" % codec)

            break

        if codec not in ("ac3", "liba52", "mp2"):
            message = (False, "aCodec %s not compatible" % codec)
            break

        if not vInfo.aKbps or int(vInfo.aKbps) > getMaxAudioBR(tsn):
            message = (False, "%s kbps exceeds max audio bitrate" % vInfo.aKbps)
            break

        audio_lang = get_tsn("audio_lang", tsn)
        if audio_lang:
            if (
                vInfo.mapAudio is None
                or vInfo.mapAudio[0][0] != select_audiolang(inFile, tsn)[-3:]
            ):
                message = (False, "%s preferred audio track exists" % audio_lang)
        break

    return message


def tivo_compatible_container(
    vInfo: VideoInfo, inFile: str, mime: str = ""
) -> Tuple[bool, str]:
    message = (True, "")
    container = vInfo.container
    if (mime == "video/x-tivo-mpeg-ts" and container != "mpegts") or (
        mime in ["video/x-tivo-mpeg", "video/mpeg", ""]
        and (container != "mpeg" or vInfo.vCodec == "mpeg1video")
    ):
        message = (False, "container %s not compatible" % container)

    return message


def tivo_compatible(inFile: str, tsn: str = "", mime: str = "") -> Tuple[bool, str]:
    vInfo = video_info(inFile)

    message = (True, "all compatible")
    if not get_bin("ffmpeg"):
        if mime not in ["video/x-tivo-mpeg", "video/mpeg", ""]:
            message = (False, "no ffmpeg")
        return message

    while True:
        vmessage = tivo_compatible_video(vInfo, tsn, mime)
        if not vmessage[0]:
            message = vmessage
            break

        amessage = tivo_compatible_audio(vInfo, inFile, tsn, mime)
        if not amessage[0]:
            message = amessage
            break

        cmessage = tivo_compatible_container(vInfo, inFile, mime)
        if not cmessage[0]:
            message = cmessage

        break

    LOGGER.debug(
        "TRANSCODE=%s, %s, %s" % (["YES", "NO"][message[0]], message[1], inFile)
    )
    return message


def audio_check(inFile: str, tsn: str) -> Optional[VideoInfo]:
    cmd_string = (
        "-y -c:v mpeg2video -r 29.97 -b:v 1000k -c:a copy "
        + select_audiolang(inFile, tsn)
        + " -t 00:00:01 -f vob -"
    )
    ffmpeg_bin = get_bin("ffmpeg")
    if ffmpeg_bin is None:
        LOGGER.error("Can't locate ffmpeg binary.")
        return None

    cmd = [ffmpeg_bin, "-i", inFile] + cmd_string.split()
    ffmpeg = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fd, testname = tempfile.mkstemp()
    testfile = os.fdopen(fd, "wb")
    try:
        shutil.copyfileobj(ffmpeg.stdout, testfile)
    except:
        kill(ffmpeg)
        testfile.close()
        vInfo = None
    else:
        testfile.close()
        vInfo = video_info(testname, False)
    os.remove(testname)
    return vInfo


def supported_format(inFile: str) -> bool:
    vInfo = video_info(inFile)
    if vInfo.Supported:
        return True
    else:
        LOGGER.debug("FALSE, file not supported %s" % inFile)
        return False


def kill(popen: subprocess.Popen) -> None:
    LOGGER.debug("killing pid=%s" % str(popen.pid))
    if sys.platform == "win32":
        win32kill(popen.pid)
    else:
        import os
        import signal

        for i in range(3):
            LOGGER.debug("sending SIGTERM to pid: %s" % popen.pid)
            os.kill(popen.pid, signal.SIGTERM)
            time.sleep(0.5)
            if popen.poll() is not None:
                LOGGER.debug("process %s has exited" % popen.pid)
                break
        else:
            while popen.poll() is None:
                LOGGER.debug("sending SIGKILL to pid: %s" % popen.pid)
                os.kill(popen.pid, signal.SIGKILL)
                time.sleep(0.5)


def win32kill(pid: int) -> None:
    import ctypes

    # We ignore types for the next 3 lines so that the absence of windll
    #   on non-Windows platforms is not flagged as an error
    handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)  # type: ignore
    ctypes.windll.kernel32.TerminateProcess(handle, -1)  # type: ignore
    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore


def gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a
