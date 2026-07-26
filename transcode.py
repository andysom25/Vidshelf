"""
transcode.py — converts video files to the format the widest range of Plex
clients can direct-play (no server-side transcoding): H.264 video + AAC
audio in an MP4 container.

Only re-encodes what actually needs it — a track that's already compatible
is stream-copied (zero quality loss, effectively instant). Video only gets
re-encoded if it isn't already H.264, at a high-enough CRF to be visually
indistinguishable from the source. This is what makes "fix the whole
library" tractable: most files only need a cheap remux (container/audio
fix), not a full re-encode.
"""

import os
import json
import shutil
import subprocess
import logging

_log = logging.getLogger('transcode')

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.webm')
TARGET_EXT = '.mp4'
COMPATIBLE_VIDEO_CODECS = ('h264',)
COMPATIBLE_AUDIO_CODECS = ('aac',)

# Visually lossless/near-lossless for x264 — chosen as a quality target
# (not a bitrate target) so encode quality doesn't degrade on complex scenes.
_X264_CRF = '17'
_X264_PRESET = 'slow'
_AAC_BITRATE = '256k'

_PROBE_TIMEOUT = 30
_CONVERT_TIMEOUT = 3600  # 1 hour per file — generous safety net, not a real-world limit for music-video-length content


def _ffmpeg_bin():
    override = os.environ.get('FFMPEG_PATH')
    return os.path.join(override, 'ffmpeg') if override else 'ffmpeg'


def _ffprobe_bin():
    override = os.environ.get('FFMPEG_PATH')
    return os.path.join(override, 'ffprobe') if override else 'ffprobe'


def _check_binary(bin_getter, version_flag='-version'):
    """Resolve a binary (respecting FFMPEG_PATH the same way _ffmpeg_bin()/
    _ffprobe_bin() do) and report whether it's actually runnable, not just
    present on PATH — a corrupt/non-executable binary would otherwise look
    identical to a healthy one from a bare shutil.which() check."""
    binary = bin_getter()
    path = shutil.which(binary)
    if not path and os.path.isfile(binary):
        path = binary
    if not path:
        return {'found': False, 'path': None, 'version': None}
    try:
        result = subprocess.run([binary, version_flag], capture_output=True, text=True, timeout=10)
        first_line = (result.stdout or result.stderr or '').splitlines()[0] if (result.stdout or result.stderr) else ''
        version = None
        if 'version' in first_line:
            # e.g. "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 ..."
            after = first_line.split('version', 1)[1].strip()
            version = after.split(' ')[0] if after else None
        return {'found': True, 'path': path, 'version': version}
    except Exception as exc:
        _log.warning("Found %s at %s but it failed to run: %s", binary, path, exc)
        return {'found': True, 'path': path, 'version': None}


def check_dependencies():
    """Report on the external binaries this module needs. Used by the
    Settings page's System Health panel — most useful for the local
    (non-Docker) install path, since the Docker image always bakes these
    in, but even there this catches a misconfigured FFMPEG_PATH."""
    return {
        'ffmpeg': _check_binary(_ffmpeg_bin),
        'ffprobe': _check_binary(_ffprobe_bin),
    }


def probe_media(path):
    """Return {'container': ext, 'video_codec': ..., 'audio_codec': ...},
    or None if ffprobe fails (missing/corrupt/unreadable file)."""
    try:
        result = subprocess.run(
            [_ffprobe_bin(), '-v', 'quiet', '-print_format', 'json', '-show_streams', path],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT
        )
        if result.returncode != 0:
            _log.warning("ffprobe exited %d for %s: %s", result.returncode, path, result.stderr[-300:])
            return None
        data = json.loads(result.stdout)
    except Exception as exc:
        _log.warning("ffprobe failed for %s: %s", path, exc)
        return None

    video_codec = None
    audio_codec = None
    for stream in data.get('streams', []):
        codec_type = stream.get('codec_type')
        if codec_type == 'video' and video_codec is None:
            video_codec = stream.get('codec_name')
        elif codec_type == 'audio' and audio_codec is None:
            audio_codec = stream.get('codec_name')

    return {
        'container': os.path.splitext(path)[1].lower(),
        'video_codec': video_codec,
        'audio_codec': audio_codec,
    }


def needs_conversion(path):
    """True if this file isn't already Plex-direct-play-friendly (MP4
    container, H.264 video, AAC audio — or no audio track at all). Returns
    False (leave it alone) if it can't even be probed, rather than guessing."""
    info = probe_media(path)
    if info is None:
        return False
    if info['container'] != TARGET_EXT:
        return True
    if info['video_codec'] not in COMPATIBLE_VIDEO_CODECS:
        return True
    if info['audio_codec'] is not None and info['audio_codec'] not in COMPATIBLE_AUDIO_CODECS:
        return True
    return False


def convert_to_plex_compatible(src_path, dest_path=None):
    """Convert src_path to MP4/H.264/AAC, writing to dest_path (defaults to
    src_path with a .mp4 extension). Stream-copies whichever track(s) are
    already compatible instead of re-encoding both unconditionally.

    Assumes src_path and dest_path are on local (fast) storage — this
    function does no NAS-safety staging of its own; see
    convert_file_safely() for converting a file that lives on a network
    mount.

    Returns {'success', 'output_path', 'video_recoded', 'audio_recoded', 'error'}.
    """
    info = probe_media(src_path)
    if info is None:
        return {'success': False, 'output_path': None, 'error':
                 'ffprobe failed — file may be corrupt or unreadable'}

    if dest_path is None:
        dest_path = os.path.splitext(src_path)[0] + TARGET_EXT

    video_ok = info['video_codec'] in COMPATIBLE_VIDEO_CODECS
    audio_ok = info['audio_codec'] is None or info['audio_codec'] in COMPATIBLE_AUDIO_CODECS

    if video_ok and audio_ok and os.path.normpath(dest_path) == os.path.normpath(src_path):
        # Already fully compatible and no rename needed — nothing to do.
        return {'success': True, 'output_path': src_path, 'video_recoded': False, 'audio_recoded': False, 'error': None}

    video_args = ['-c:v', 'copy'] if video_ok else ['-c:v', 'libx264', '-crf', _X264_CRF, '-preset', _X264_PRESET]
    audio_args = ['-c:a', 'copy'] if audio_ok else ['-c:a', 'aac', '-b:a', _AAC_BITRATE]

    # Write to a same-directory temp file first, then os.replace() into
    # place — an in-place ffmpeg run reading and writing the same path
    # corrupts the output. A same-directory temp file also means the final
    # swap is a same-filesystem rename (cheap, atomic), not a cross-
    # filesystem copy.
    tmp_path = dest_path + '.converting.mp4'

    cmd = [_ffmpeg_bin(), '-y', '-i', src_path,
           '-map', '0:v:0', '-map', '0:a:0?',
           *video_args, *audio_args,
           '-movflags', '+faststart', tmp_path]

    _log.info("Converting %s -> %s (video %s, audio %s)", src_path, dest_path,
              'copy' if video_ok else f'libx264 crf={_X264_CRF}',
              'copy' if audio_ok else f'aac {_AAC_BITRATE}')

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_CONVERT_TIMEOUT)
    except Exception as exc:
        _remove_if_exists(tmp_path)
        return {'success': False, 'output_path': None, 'error': str(exc)}

    if result.returncode != 0 or not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
        _remove_if_exists(tmp_path)
        return {'success': False, 'output_path': None,
                'error': f'ffmpeg exited {result.returncode}: {result.stderr[-500:]}'}

    os.replace(tmp_path, dest_path)
    if os.path.normpath(dest_path) != os.path.normpath(src_path):
        _remove_if_exists(src_path)

    return {
        'success': True,
        'output_path': dest_path,
        'video_recoded': not video_ok,
        'audio_recoded': not audio_ok,
        'error': None,
    }


def _remove_if_exists(path):
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _safe_copy(src, dst):
    """Manual buffered copy — never shutil.copy2/copyfile. See CLAUDE.md
    gotcha #2: os.sendfile() (shutil's internal fast-path on Linux) is
    unreliable against this project's CIFS-mounted NAS share."""
    with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
        shutil.copyfileobj(fsrc, fdst)


def convert_file_safely(path, scratch_dir):
    """Convert a video file that may live on a network mount, staging both
    the read and the write through local scratch storage first.

    ffmpeg's own I/O should be fine directly against a CIFS mount (it
    doesn't use the sendfile() fast-path that gotcha #2 warns about), but
    this is a bulk, unattended job that can touch a whole library — staging
    locally costs a bit of extra disk I/O in exchange for not finding out
    the hard way, on a NAS this codebase has already had CIFS-mount
    surprises with (see CLAUDE.md).

    Returns the same shape as convert_to_plex_compatible(), with
    'output_path' rewritten back to the final NAS-side location.
    """
    os.makedirs(scratch_dir, exist_ok=True)
    basename = os.path.basename(path)
    local_src = os.path.join(scratch_dir, basename)

    try:
        _safe_copy(path, local_src)
    except Exception as exc:
        _remove_if_exists(local_src)
        return {'success': False, 'output_path': None, 'error': f'Failed staging copy from source: {exc}'}

    result = convert_to_plex_compatible(local_src)
    if not result['success']:
        _remove_if_exists(local_src)
        return result

    local_output = result['output_path']
    final_dest = os.path.join(os.path.dirname(path), os.path.basename(local_output))
    try:
        _safe_copy(local_output, final_dest)
        try:
            shutil.copystat(local_output, final_dest)
        except Exception:
            pass  # cosmetic — some NAS filesystems reject utime/chmod, not fatal
    except Exception as exc:
        _remove_if_exists(local_src)
        _remove_if_exists(local_output)
        return {'success': False, 'output_path': None, 'error': f'Failed copying converted file back: {exc}'}

    _remove_if_exists(local_src)
    _remove_if_exists(local_output)

    if os.path.normpath(final_dest) != os.path.normpath(path):
        try:
            os.remove(path)
        except Exception as exc:
            _log.warning("Converted %s but failed to remove original %s: %s", final_dest, path, exc)

    result['output_path'] = final_dest
    return result
