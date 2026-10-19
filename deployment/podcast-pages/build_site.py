#!/usr/bin/env python3
"""Build only public feed files and authorized Release audio; never generate speech.

Canonical checkout files are read-only. A complete validated staging directory
replaces _site only after every download and size check succeeds.
"""
import argparse
from contextlib import closing
from http.client import HTTPException
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

REPOSITORY = 'emily3311/tingye-player'
SITE = 'https://emily3311.github.io/tingye-player'
MAX_BYTES = 1_000_000_000  # conservative decimal 1 GB, strictly less
STATIC = ('index.html', 'cover.png', 'cover-emily-go-go-go.png', 'robots.txt', '.nojekyll', 'README.md')
ASSET = re.compile(r'https://github\.com/emily3311/tingye-player/releases/download/'
                   r'audio-\d{4}-(?:0[1-9]|1[0-2])/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.mp3\Z')


class BuildError(RuntimeError):
    pass


class ReleaseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parsed = urlsplit(newurl)
            port = parsed.port
        except ValueError:
            raise BuildError('Release 音频跳转地址无效。') from None
        if (parsed.scheme != 'https' or parsed.hostname != 'release-assets.githubusercontent.com'
                or parsed.username or parsed.password or port not in (None, 443)
                or '\\' in newurl):
            raise BuildError('Release 音频跳转到未授权主机，已停止构建。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_release(url):
    return build_opener(ReleaseRedirect()).open(
        Request(url, headers={'User-Agent': 'tingye-podcast-pages/1'}), timeout=30)


def _download(url, target, expected, fetch):
    for attempt in range(3):
        try:
            received = 0
            with closing(fetch(url)) as response, target.open('wb') as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected:
                        raise BuildError('音频长度超过 RSS 声明，已停止构建。')
                    output.write(chunk)
            if received != expected:
                raise BuildError('音频长度与 RSS 声明不一致，已停止构建。')
            return
        except (OSError, HTTPException, BuildError):
            target.unlink(missing_ok=True)
            if attempt == 2:
                raise BuildError('音频下载三次仍未成功或长度不符；未部署任何新文件。') from None


def compact_mp3(source, target):
    """Make a speech copy while retaining the canonical Release asset."""
    def duration(path):
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
            'format=duration', '-of', 'json', str(path)], check=True,
            capture_output=True, text=True, timeout=60)
        value = float(json.loads(result.stdout)['format']['duration'])
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid duration')
        return value
    try:
        before = duration(source)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-y', '-i', str(source),
            '-map', '0:a:0', '-vn', '-ar', '24000', '-ac', '1', '-codec:a',
            'libmp3lame', '-b:a', '48k', str(target)], check=True,
            capture_output=True, timeout=max(120, before))
        if abs(duration(target) - before) > 1:
            raise ValueError('Duration changed')
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(target),
            '-f', 'null', '-'], check=True, capture_output=True, timeout=max(120, before))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        raise BuildError('播放副本转码或完整性检查失败，未部署任何新文件。') from None


def build_site(source, destination, *, fetch=fetch_release, max_bytes=MAX_BYTES, compact_audio=False):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if destination.is_symlink() or destination.resolve() == source or destination.resolve() in source.parents:
        raise BuildError('构建输出目录无效。')
    if not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_BYTES:
        raise BuildError('站点容量上限必须不超过 1 GB。')
    feed = source / 'feed.xml'
    if feed.is_symlink() or not feed.is_file() or feed.stat().st_size > 8_000_000:
        raise BuildError('缺少有效的公开 feed.xml，或订阅源过大。')
    original = feed.read_bytes()
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', original, re.I):
        raise BuildError('订阅源不允许包含 DTD 或实体声明。')
    try:
        for _, pair in ET.iterparse(io.BytesIO(original), events=['start-ns']):
            prefix, uri = pair
            if not re.fullmatch(r'ns\d+', prefix):
                ET.register_namespace(prefix, uri)
        tree = ET.fromstring(original, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    except (ET.ParseError, ValueError):
        raise BuildError('公开订阅源 XML 无效。') from None
    if tree.tag != 'rss' or len(tree.findall('channel')) != 1:
        raise BuildError('订阅源必须包含一个 RSS channel。')
    downloads = []
    seen = set()
    items = tree.findall('./channel/item')
    if not items:
        raise BuildError('订阅源没有可发布的音频。')
    for item in items:
        enclosures = item.findall('enclosure')
        if len(enclosures) != 1:
            raise BuildError('每期必须且只能包含一个音频附件。')
        enclosure = enclosures[0]
        match = ASSET.fullmatch(enclosure.get('url', ''))
        size = enclosure.get('length', '')
        if not match or not re.fullmatch(r'[1-9]\d{0,18}', size) or enclosure.get('type') != 'audio/mpeg':
            raise BuildError('RSS 音频必须来自已授权仓库的按月 Release MP3，且长度与类型有效。')
        identity = match[1]
        if identity in seen:
            raise BuildError('订阅源包含重复音频编号。')
        seen.add(identity)
        downloads.append((enclosure.get('url'), identity+'.mp3', int(size)))
        enclosure.set('url', f'{SITE}/audio/{identity}.mp3')
    if len(list(tree.iter('enclosure'))) != len(downloads):
        raise BuildError('订阅源存在非标准位置的音频附件。')
    rendered = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
    files = []
    total = len(rendered) + sum(size for _, _, size in downloads)
    for name in STATIC:
        file = source/name
        if file.is_symlink():
            raise BuildError('公开站点文件不允许使用符号链接。')
        if file.exists():
            if not file.is_file():
                raise BuildError('公开站点文件格式无效。')
            files.append((file, name))
            total += file.stat().st_size
    if total >= max_bytes and not compact_audio:
        raise BuildError('站点必须严格小于 1 GB；请减少音频或改用专用播客托管。未部署部分内容。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.podcast-pages-', dir=destination.parent) as temporary:
        staging = Path(temporary)/'site'
        (staging/'audio').mkdir(parents=True)
        for file, name in files:
            shutil.copyfile(file, staging/name)
        (staging/'feed.xml').write_bytes(rendered)
        manifest = {}
        for item, (url, filename, size) in zip(items, downloads):
            if compact_audio:
                original = Path(temporary)/filename
                _download(url, original, size, fetch)
                target = staging/'audio'/filename
                target.parent.mkdir(exist_ok=True)
                compact_mp3(original, target)
                original.unlink()
                media_url = f'{SITE}/audio/{filename}'
                length = target.stat().st_size
                item.find('enclosure').set('url', media_url)
                item.find('enclosure').set('length', str(length))
                manifest[filename[:-4]] = {'source_bytes': size, 'bytes': length, 'url': media_url}
            else:
                _download(url, staging/'audio'/filename, size, fetch)
        if compact_audio:
            (staging/'feed.xml').write_bytes(ET.tostring(tree, encoding='utf-8', xml_declaration=True))
            (staging/'media.json').write_text(json.dumps(manifest, sort_keys=True))
        actual = sum(file.stat().st_size for file in staging.rglob('*') if file.is_file())
        if actual >= max_bytes:
            raise BuildError('完整站点超过 1 GB 限制，未部署部分内容。')
        if destination.exists():
            if not destination.is_dir():
                raise BuildError('构建输出必须是目录。')
            shutil.rmtree(destination)
        staging.replace(destination)
    return {'episodes': len(downloads), 'bytes': actual}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='.')
    parser.add_argument('--output', default='_site')
    parser.add_argument('--compact-audio', action='store_true', help='Publish verified 48 kbps mono speech copies')
    args = parser.parse_args()
    try:
        result = build_site(args.source, args.output, compact_audio=args.compact_audio)
    except (BuildError, OSError) as error:
        parser.exit(1, f'播客站点构建失败：{error}\n' if isinstance(error, BuildError) else '播客站点构建失败：无法读写构建文件。\n')
    print(f"Built {result['episodes']} episode(s), {result['bytes']} bytes; canonical feed unchanged.")


if __name__ == '__main__':
    main()
