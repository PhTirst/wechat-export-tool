"""Render a self-contained, paginated local conversation viewer.

Only the passed conversation is serialized. No database or key access occurs here.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .security import private_text_writer

_REASONS = {
    "emoji_md5_unavailable": "消息缺少可定位的表情标识",
    "unrecognized_image_format": "图片尚未解密或格式暂不支持",
    "image_decode_validation_failed": "图片解码未通过校验",
    "image_key_or_payload_invalid": "图片密钥不匹配或文件不完整",
    "image_key_unavailable": "暂未取得此图片所需的解密密钥",
    "image_xor_key_unavailable": "暂未取得此图片所需的异或密钥",
    "invalid_dat_header": "图片文件头无效或格式暂不支持",
    "local_image_reference_unavailable": "消息中没有有效的本机图片引用",
    "ambiguous_raw_message": "原始消息无法唯一匹配，未猜测附件来源",
    "local_file_read_failed": "本机附件读取失败",
    "local_file_not_found": "本机未找到附件原文件",
}


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _date(message: dict) -> tuple[str, str]:
    iso = _text(message.get("ts_iso"))
    if len(iso) >= 10 and iso[4:5] == "-" and iso[7:8] == "-":
        return iso[:10], iso[:19].replace("T", " ")
    try:
        stamp = float(message.get("ts", 0))
        if stamp > 1e11:
            stamp /= 1000
        local = datetime.fromtimestamp(stamp, timezone(timedelta(hours=8)))
        return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "", "时间未知"


def _safe_path(value: object) -> str:
    raw = _text(value).replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or not path.parts or path.is_absolute() or any(
        not part or part in ("..", ".") or ":" in part for part in raw.split("/")
    ) or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return ""
    return path.as_posix()


def _media(items: object) -> list[dict]:
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind", "file")
        kind = kind if kind in ("image", "audio", "video", "file") else "file"
        path = _safe_path(item.get("path"))
        available = item.get("status") == "available" and bool(path)
        reason = _text(item.get("reason"))
        result.append({
            "kind": kind,
            "path": path,
            "name": _text(item.get("name")) or (PurePosixPath(path).name if path else "附件"),
            "mime": _text(item.get("mime")),
            "status": "available" if available else "missing",
            "reason": _REASONS.get(reason, reason) or (
                "" if available else "本机未找到可用原文件"
            ),
            "original_path": _safe_path(item.get("original_path")),
            "poster": _safe_path(item.get("poster")),
            "thumbnail": bool(item.get("thumbnail")),
        })
    return result


def render_viewer(messages: list[dict], output_html: Path, summary: dict) -> int:
    """Write an offline HTML with protected ACL; return the message count.

    ``media`` entries use paths relative to the HTML. Attachment counts count
    message references, so two messages referencing one file count twice.
    ``summary`` may provide ``conversation`` (or ``target``), ``note`` and
    ``warning`` for the visible heading and export notes.
    """
    output_html = Path(output_html)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    count = len(messages)
    available = missing = 0
    for message in messages:
        for item in _media(message.get("media")):
            if item["status"] == "available":
                available += 1
            else:
                missing += 1
    title = _text(summary.get("conversation") or summary.get("target") or "聊天记录")
    note = "；".join(_text(summary.get(key)) for key in ("note", "warning") if summary.get(key))
    nonce = base64.b64encode(os.urandom(18)).decode("ascii")
    header = _HEADER.replace("__NONCE__", nonce)
    header = header.replace("__COUNT__", f"{count:,}").replace("__AVAILABLE__", f"{available:,}")
    header = header.replace("__MISSING__", f"{missing:,}")
    with private_text_writer(output_html, secure_parent=True) as handle:
        handle.write(header)
        handle.write('<script id="viewer-info" type="application/json">')
        handle.write(json.dumps({"title": title, "note": note}, ensure_ascii=False).replace("<", "\\u003c"))
        handle.write('</script>')
        handle.write('<script id="conversation-data" type="application/json">[')
        for number, message in enumerate(messages):
            day, display_time = _date(message)
            row = [day, display_time, _text(message.get("sender")), bool(message.get("is_self")),
                   _text(message.get("content")), _text(message.get("msg_type")), _media(message.get("media"))]
            if number:
                handle.write(",")
            # Escape '<' even in application/json to prevent an injected closing
            # script element. The live UI uses textContent for every data value.
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c"))
        handle.write(']</script><script nonce="' + nonce + '">')
        handle.write(_SCRIPT)
        handle.write("</script></body></html>")
    return count


_HEADER = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; media-src file:; style-src 'unsafe-inline'; script-src 'nonce-__NONCE__'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>离线聊天记录</title><style>
:root{color-scheme:light;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;color:#26352d;background:#edf1ee}
*{box-sizing:border-box}body{margin:0}header,.toolbar,.pager,#list,.footnote{max-width:1100px;margin:auto}
header{padding:28px 22px 18px}h1{font-size:26px;margin:0 0 8px}.stats{display:flex;gap:12px;flex-wrap:wrap}
.stats span{background:white;border-radius:7px;padding:5px 12px}.notice,.extra{color:#63746a;font-size:13px;margin:10px 0 0}
.toolbar{background:#fff;padding:14px 22px;display:flex;flex-wrap:wrap;gap:10px;align-items:end;border-radius:12px}
label{display:grid;gap:3px;font-size:13px}input,select,button{font:inherit;padding:6px 10px;border:1px solid #cad6ce;border-radius:6px;background:white;color:inherit}
input[type=search]{width:230px}button{cursor:pointer}button.primary{background:#198754;color:white;border-color:#198754}button:disabled{opacity:.4;cursor:default}
.pager{padding:14px 22px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}.pager input{width:78px}.pager .count{margin-right:auto;color:#586b5e}
#list{min-height:180px;padding:0 22px 24px}.day{text-align:center;color:#77887d;font-size:13px;margin:22px 0 16px}
.message{display:flex;margin:12px 0;gap:10px;align-items:flex-start}.message.self{flex-direction:row-reverse}
.avatar{width:34px;height:34px;flex:none;background:#d7e1d9;color:#52705c;display:grid;place-items:center;border-radius:8px;font-size:13px}
.self .avatar{background:#b8dbc1}.body{max-width:min(78%,760px);min-width:100px}.meta{font-size:12px;color:#708174;margin-bottom:4px;overflow-wrap:anywhere}
.self .meta{text-align:right}.bubble{background:#fff;border-radius:9px;padding:10px 13px;box-shadow:0 1px 2px #163c2110;overflow-wrap:anywhere}
.self .bubble{background:#a9e98c}.content{white-space:pre-wrap}.type{font-size:11px;color:#6e7d71;margin-top:6px}
.attachment{margin-top:9px;border-top:1px solid #65816926;padding-top:9px}.attachment img{display:block;max-width:100%;max-height:500px;object-fit:contain;border-radius:6px;cursor:zoom-in}
audio{width:330px;max-width:100%}video{max-width:100%;max-height:450px;display:block;border-radius:6px}a{color:#126e48;overflow-wrap:anywhere}
.filename{font-size:12px;margin:4px 0}.missing{font-size:13px;background:#fff5de;color:#785a21;border:1px solid #ead5aa;padding:7px 10px;border-radius:6px}
.media-error{font-size:12px;color:#854a20;margin:5px 0}.empty{padding:70px 0;text-align:center;color:#6d7b71}.footnote{padding:0 22px 24px;color:#728077;font-size:12px}
dialog{border:0;border-radius:12px;max-width:96vw;max-height:96vh;padding:12px;background:#fff}dialog::backdrop{background:#000b}
dialog img{display:block;max-width:90vw;max-height:82vh;object-fit:contain}.lightbox-controls{display:flex;justify-content:end;margin-bottom:8px}
@media(max-width:650px){header{padding-top:18px}h1{font-size:22px}.toolbar,#list,.pager{padding-left:12px;padding-right:12px}.body{max-width:86%}.avatar{display:none}.stats{gap:5px}.stats span{font-size:12px;padding:4px 7px}input[type=search]{width:190px}.pager{gap:6px}.pager button{padding:5px 7px}}
</style></head><body><header><h1 id="conversation-title">聊天记录</h1>
<div class="stats"><span>消息 __COUNT__ 条</span><span>可用附件 __AVAILABLE__ 项</span><span>缺失或不可预览 __MISSING__ 项</span></div>
<p class="notice">此网页及附件均保存在本机，可离线使用。附件只覆盖本机仍存在且可解码的内容，可能不是完整历史；统计按消息中的附件引用计数。请保持网页与附件目录的相对位置。</p>
<p class="extra" id="export-note"></p></header>
<form id="filters" class="toolbar"><label>关键词（正文或发送者）<input id="keyword" type="search" placeholder="搜索聊天记录" autocomplete="off"></label>
<label>开始日期<input id="from" type="date"></label><label>结束日期<input id="to" type="date"></label>
<button class="primary" type="submit">筛选</button><button id="reset" type="button">清空</button>
<label>每页<select id="size"><option>100</option><option selected>150</option><option>200</option></select></label></form>
<nav class="pager" aria-label="分页"><span class="count" id="result" role="status" aria-live="polite">正在载入…</span>
<button id="previous" type="button">上一页</button><label>页码<input id="page" type="number" min="1" value="1" aria-label="页码"></label>
<span id="pages"></span><button id="go" type="button">跳转</button><button id="next" type="button">下一页</button>
<label>跳到日期<input id="jump-date" type="date"></label><button id="jump" type="button">定位</button></nav>
<main id="list" aria-label="聊天消息"></main><nav class="pager" aria-label="底部分页">
<button id="bottom-previous" type="button">上一页</button><span id="bottom-status"></span><button id="bottom-next" type="button">下一页</button><button id="top" type="button">回到顶部</button></nav>
<p class="footnote">时间按导出记录显示。图片可点击放大；音视频点击后才加载。浏览器不能解码的媒体仍可下载，使用本机播放器打开。</p>
<dialog id="lightbox"><div class="lightbox-controls"><button type="button" id="close-lightbox">关闭</button></div><img alt="聊天图片大图" id="large-image"></dialog>
'''

_SCRIPT = r'''
'use strict';
const $ = id => document.getElementById(id);
const infoNode = $('viewer-info');
const info = JSON.parse(infoNode.textContent);
infoNode.remove();
$('conversation-title').textContent = info.title;
$('export-note').textContent = info.note;
document.title = `${info.title} · 离线聊天记录`;
const dataNode = $('conversation-data');
const messages = JSON.parse(dataNode.textContent);
dataNode.remove();
let selected = Array.from({length:messages.length}, (_,i)=>i);
let current = 1, size = 150, filterGeneration = 0;
const names = {image:'图片',audio:'语音',video:'视频',file:'文件'};
function element(tag, className, text) {
  const node = document.createElement(tag);
  if(className) node.className = className;
  if(text !== undefined) node.textContent = text;
  return node;
}
function localURL(path) {
  const parts = String(path || '').replaceAll('\\','/').split('/');
  if(!parts.length || parts.some(p=>!p || p==='..' || p==='.' || p.includes(':'))) return null;
  return parts.map(encodeURIComponent).join('/');
}
function downloadLink(holder,path,name,label) {
  const url = localURL(path); if(!url) return;
  const link = element('a','filename',label);
  link.href = url; link.download = name || ''; link.target = '_blank'; link.rel = 'noopener';
  const line = element('div','filename'); line.append(link); holder.append(line);
}
function attachment(item) {
  const holder = element('div','attachment');
  const url = localURL(item.path);
  if(item.status !== 'available' || !url) {
    holder.append(element('div','missing',`${names[item.kind]||'附件'}${item.original_path?'预览不可用':'未导出'} · ${item.reason||'本机原文件缺失'}${item.name?' · '+item.name:''}`));
    downloadLink(holder,item.original_path,'','下载已保留的原编码文件（可能需要专用程序）');
    return holder;
  }
  let player;
  if(item.kind === 'image') {
    player = element('img'); player.alt = item.name || '聊天图片';
    player.loading = 'lazy'; player.decoding = 'async'; player.src = url;
    player.tabIndex = 0; player.setAttribute('role','button');
    const open = ()=>{ $('large-image').src = url; $('lightbox').showModal(); };
    player.addEventListener('click',open);
    player.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();open();}});
  } else if(item.kind === 'audio' || item.kind === 'video') {
    player = element(item.kind); player.controls = true; player.preload = 'none'; player.src = url;
    if(item.kind === 'video') { player.playsInline = true; const poster=localURL(item.poster); if(poster) player.poster=poster; }
  }
  if(player) {
    player.addEventListener('error',()=>{
      if(!holder.querySelector('.media-error')) holder.append(element('p','media-error','浏览器无法加载或解码此附件，可下载后用本机程序打开。'));
    });
    holder.append(player);
  }
  if(item.reason || item.thumbnail) holder.append(element('p','filename',item.reason || '仅找到缩略图'));
  downloadLink(holder,item.path,item.name,`${item.kind==='file'?'📎 ':''}${item.name || names[item.kind] || '附件'} · 下载`);
  if(item.original_path && item.original_path!==item.path) downloadLink(holder,item.original_path,'','下载原编码文件（可能需要专用程序）');
  return holder;
}
function render(scroll = false) {
  const pages = Math.max(1,Math.ceil(selected.length/size));
  current = Math.max(1,Math.min(current,pages));
  const start = (current-1)*size, end = Math.min(start+size,selected.length);
  // Stop playback before replacing the current page's bounded DOM.
  $('list').querySelectorAll('audio,video').forEach(media=>{media.pause(); media.removeAttribute('src'); media.load();});
  const fragment = document.createDocumentFragment();
  let day = null;
  for(let index=start;index<end;index++) {
    const row = messages[selected[index]];
    if(row[0] !== day) { day = row[0]; fragment.append(element('div','day',day || '日期未知')); }
    const article = element('article',row[3]?'message self':'message');
    article.append(element('div','avatar',row[3]?'我':(row[2]||'对方').slice(0,2)));
    const body = element('div','body');
    body.append(element('div','meta',`${row[2] || (row[3]?'我':'对方')} · ${row[1]}`));
    const bubble = element('div','bubble');
    if(row[4]) bubble.append(element('div','content',row[4]));
    for(const item of row[6]) bubble.append(attachment(item));
    if(!row[4] && !row[6].length) bubble.append(element('div','content','[无可显示的正文]'));
    if(row[5] && row[5] !== '1' && row[5] !== 'text') bubble.append(element('div','type',`消息类型：${row[5]}`));
    body.append(bubble); article.append(body); fragment.append(article);
  }
  if(!selected.length) fragment.append(element('div','empty','没有符合条件的消息。请调整日期或关键词。'));
  $('list').replaceChildren(fragment);
  $('page').value = current; $('page').max = pages; $('pages').textContent = `/ ${pages} 页`;
  $('result').textContent = `匹配 ${selected.length.toLocaleString()} / ${messages.length.toLocaleString()} 条 · 当前 ${selected.length?start+1:0}–${end} 条`;
  $('bottom-status').textContent = `第 ${current} / ${pages} 页`;
  for(const id of ['previous','bottom-previous']) $(id).disabled = current <= 1;
  for(const id of ['next','bottom-next']) $(id).disabled = current >= pages;
  if(scroll) $('filters').scrollIntoView({block:'start'});
}
async function applyFilters(event) {
  if(event) event.preventDefault();
  const generation = ++filterGeneration;
  const needle = $('keyword').value.trim().toLocaleLowerCase(), from = $('from').value, to = $('to').value;
  if(from && to && from > to) { $('result').textContent = '开始日期不能晚于结束日期。'; return; }
  $('result').textContent = '正在筛选…';
  const result = [];
  for(let index=0;index<messages.length;index++) {
    const row = messages[index];
    if((!from || row[0]>=from) && (!to || (row[0] && row[0]<=to)) &&
       (!needle || row[4].toLocaleLowerCase().includes(needle) || row[2].toLocaleLowerCase().includes(needle))) result.push(index);
    if(index % 5000 === 4999) { await new Promise(resolve=>setTimeout(resolve,0)); if(generation!==filterGeneration) return; }
  }
  selected = result; current = 1; render();
}
$('filters').addEventListener('submit',applyFilters);
$('reset').addEventListener('click',()=>{ $('keyword').value=''; $('from').value=''; $('to').value=''; applyFilters(); });
$('size').addEventListener('change',()=>{const first=(current-1)*size;size=Number($('size').value);current=Math.floor(first/size)+1;render();});
for(const id of ['previous','bottom-previous']) $(id).addEventListener('click',()=>{current--;render(true);});
for(const id of ['next','bottom-next']) $(id).addEventListener('click',()=>{current++;render(true);});
const goPage=()=>{current=Number.parseInt($('page').value,10)||1;render(true);};
$('go').addEventListener('click',goPage);
$('page').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();goPage();}});
$('jump').addEventListener('click',()=>{
  const target = $('jump-date').value;
  if(!target) return;
  const position = selected.findIndex(index=>messages[index][0]>=target);
  if(position<0) { $('result').textContent='当前筛选结果中没有该日期及之后的消息。'; return; }
  current=Math.floor(position/size)+1;render(true);
});
$('top').addEventListener('click',()=>window.scrollTo({top:0,behavior:'smooth'}));
$('close-lightbox').addEventListener('click',()=>$('lightbox').close());
$('lightbox').addEventListener('click',event=>{if(event.target===$('lightbox')) $('lightbox').close();});
$('lightbox').addEventListener('close',()=>$('large-image').removeAttribute('src'));
render();
'''
