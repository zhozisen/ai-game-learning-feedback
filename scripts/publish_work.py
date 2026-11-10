"""Validate a work application before writing it to the public catalog."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ApprovalError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_public_repo(owner, project):
    headers = {'User-Agent': 'class-works-validator', 'Accept': 'application/vnd.github+json'}
    if token := os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + token
    url = f'https://api.github.com/repos/{owner}/{project}'
    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=10) as response:
            repo = json.loads(response.read(65536))
    except (URLError, OSError, ValueError) as exc:
        raise ApprovalError('仓库不存在或不可公开访问，请检查源码仓库网址') from exc
    if repo.get('private') is not False or repo.get('owner', {}).get('login', '').lower() != owner.lower():
        raise ApprovalError('请使用自己账号下的公开源码仓库')
    if not repo.get('has_pages'):
        raise ApprovalError('源码仓库尚未启用 GitHub Pages')


def check_game(play_url):
    opener = build_opener(NoRedirect())
    root = play_url if play_url.endswith('/') else play_url.rsplit('/', 1)[0] + '/'
    for url, name, signature in [
        (play_url, '网页入口', None),
        (urljoin(root, 'index.js'), 'index.js', None),
        (urljoin(root, 'index.wasm'), 'index.wasm', b'\0asm'),
        (urljoin(root, 'index.pck'), 'index.pck', b'GDPC'),
    ]:
        try:
            with opener.open(Request(url, headers={'User-Agent': 'class-works-validator'}), timeout=10) as response:
                chunk = response.read(256 if signature is None else len(signature))
                if not chunk or (signature and chunk != signature):
                    raise ApprovalError(f'{name}内容与预期不符')
                content_type = response.headers.get_content_type()
                if url == play_url and content_type != 'text/html':
                    raise ApprovalError('试玩入口应返回 HTML 网页')
                if name == 'index.js' and content_type == 'text/html':
                    raise ApprovalError('index.js 返回了 HTML 页面')
        except (URLError, OSError) as exc:
            raise ApprovalError(f'无法读取{name}，请核对公开地址及完整导出文件') from exc


def approve(event, reviewer):
    issue = event['issue']
    applicant = issue.get('user', {}).get('login', '').lower()
    sender = event.get('sender', {})
    actor = sender.get('login', '').lower()
    automatic = event.get('action') in {'opened', 'edited', 'reopened'}
    if automatic:
        if not applicant or actor != applicant or sender.get('type') != 'User' or issue.get('state') != 'open':
            raise ApprovalError('请使用申请人的 GitHub 账号提交或更新开放申请')
    elif event.get('label', {}).get('name') != '通过上架' or actor != reviewer.lower():
        raise ApprovalError('申请事件不符合自动检查条件')
    if not issue['title'].startswith('[作品申请]') or 'pull_request' in issue:
        raise ApprovalError('请使用「[作品申请]」开头的 Issue')
    matches = re.findall(r'```json[ \t]*\r?\n(.*?)\r?\n```', issue.get('body') or '', flags=re.DOTALL)
    if len(matches) != 1 or len(matches[0]) > 12000:
        raise ApprovalError('请在申请正文放入一个完整的 JSON 代码块')
    try:
        data = json.loads(matches[0])
    except ValueError as exc:
        raise ApprovalError('申请 JSON 格式有误') from exc
    if not isinstance(data, dict):
        raise ApprovalError('申请 JSON 应为一组作品字段')
    item = {}
    for key, limit in {'title':80,'author':30,'description':1500,'controls':1000,'version':80,'repoUrl':300,'playUrl':500}.items():
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ApprovalError(f'请核对申请字段 {key}')
        item[key] = value.strip()
    repo = re.fullmatch(r'https://github[.]com/([a-zA-Z0-9-]+)/([a-zA-Z0-9_.-]+)/?', item['repoUrl'])
    if not repo or repo[2] in ('.', '..'):
        raise ApprovalError('请填写完整的 GitHub 源码仓库网址')
    owner, project = repo.groups()
    if automatic and owner.lower() != applicant:
        raise ApprovalError('仓库归属与申请账号不一致，请提交自己 GitHub 账号下的仓库')
    url = urlsplit(item['playUrl'])
    if project.lower() == f'{owner.lower()}.github.io':
        prefix = '/'
    else:
        prefix = '/' + project + '/'
    if (url.scheme != 'https' or url.netloc.lower() != f'{owner.lower()}.github.io'
            or url.query or url.fragment or not url.path.startswith(prefix)
            or not re.fullmatch(r'(?:[a-zA-Z0-9_-]+/)*(?:index[.]html)?', url.path[len(prefix):])):
        raise ApprovalError('试玩地址须指向源码仓库对应的 GitHub Pages 目录或 index.html')
    if automatic:
        check_public_repo(owner, project)
    check_game(item['playUrl'])
    item.update(
        id='student-' + hashlib.sha256(f'{owner}/{project}'.lower().encode()).hexdigest()[:24],
        kind='student', approved=True, reviewedAt=datetime.now(timezone.utc).date().isoformat(),
        applicationNumber=issue['number'],
        applicationUpdatedAt=issue.get('updated_at', ''),
    )
    return item


def merge_work(works, item):
    def order(work):
        return work.get('applicationNumber', 0), work.get('applicationUpdatedAt', '')

    for index, other in enumerate(works):
        if other['id'] == item['id']:
            if order(other) > order(item):
                return works
            return [*works[:index], item, *works[index + 1:]]
    return [*works, item]


def main():
    event_path, reviewer, mode, path = sys.argv[1:]
    target = Path(path)
    if mode == 'validate':
        try:
            item = approve(json.loads(Path(event_path).read_text(encoding='utf-8')), reviewer)
        except ApprovalError as exc:
            print(exc, file=sys.stderr)
            return 2
        target.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
    elif mode == 'merge':
        item = json.loads(target.read_text(encoding='utf-8'))
        catalog = Path('approved-works.json')
        works = json.loads(catalog.read_text(encoding='utf-8')) if catalog.exists() else []
        merged = merge_work(works, item)
        catalog.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    else:
        raise ValueError(f'未知模式 {mode}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
