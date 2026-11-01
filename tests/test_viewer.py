"""Synthetic-only offline viewer security and bounded-DOM regression tests."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import unittest
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechat_export_tool import render_media_viewer as viewer


class _Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__()
        self.active = None
        self.scripts: dict[str, str] = {}
        self.tags = []
        self.csp = ""
        self.feed(text)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.tags.append((tag, attrs))
        if tag == "script":
            self.active = attrs.get("id", "executable")
            self.scripts[self.active] = ""
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy":
            self.csp = attrs["content"]

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = None

    def handle_data(self, text):
        if self.active:
            self.scripts[self.active] += text


def _render(messages, summary=None):
    stream = io.StringIO()

    @contextmanager
    def writer(path, **kwargs):
        assert Path(path).name == "index.html"
        assert kwargs == {"secure_parent": True}
        yield stream

    with patch.object(viewer, "private_text_writer", writer):
        count = viewer.render_viewer(messages, Path("index.html"), summary or {"target": "Synthetic"})
    return count, stream.getvalue()


def _message(**kwargs):
    return {"ts_iso": "2026-01-01T12:00:00+08:00", "sender": "Synthetic contact",
            "is_self": False, "content": "Synthetic text", "msg_type": 1,
            "conversation_id": "synthetic-room", "chat_room": "Synthetic", **kwargs}


_MOCK_DOM = r'''
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.listeners={}; this.value=''; this.textContent=''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  addEventListener(type,callback) { (this.listeners[type] ||= []).push(callback); }
  async fire(type,extra={}) { for(const callback of this.listeners[type]||[]) await callback({target:this,preventDefault(){},...extra}); }
  setAttribute(key,value) { this[key]=value; }
  removeAttribute(key) { delete this[key]; }
  remove() { this.removed=true; }
  scrollIntoView() {}
  pause() { this.paused=true; }
  load() {}
  showModal() { this.open=true; }
  close() { this.open=false; this.fire('close'); }
  all() { return this.children.flatMap(child=>[child,...child.all()]); }
  querySelectorAll(selector) {
    const tags=selector.split(','); return this.all().filter(node=>tags.includes(node.tagName));
  }
  querySelector(selector) { return this.all().find(node=>(node.className||'').split(' ').includes(selector.slice(1)))||null; }
  set innerHTML(value) { throw Error('Untrusted innerHTML is forbidden'); }
}
const registry = {};
for(const id of ['viewer-info','conversation-title','export-note','conversation-data','list','page','pages','result','bottom-status',
'previous','bottom-previous','next','bottom-next','filters','reset','size','go','jump','jump-date','top','lightbox','large-image',
'close-lightbox','keyword','from','to']) registry[id]=new Element();
registry['viewer-info'].textContent=FIXTURE_INFO;
registry['conversation-data'].textContent=FIXTURE_ROWS;
const document={getElementById:id=>registry[id],createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment')};
const window={scrollTo(){}};
function assert(condition,message) { if(!condition) throw Error(message); }
function articles() { return registry.list.all().filter(node=>node.tagName==='article'); }
'''


class ViewerTests(unittest.TestCase):
    def test_serialization_is_inert_and_paths_are_local(self):
        attack = '</script><img src="https://example.invalid/a" onerror="alert(1)">'
        media = [
            {"kind": "image", "status": "available", "path": "media/image #1.jpg", "name": attack,
             "original_path": "media/original.wxgf", "thumbnail": True, "reason": "仅找到缩略图"},
            {"kind": "video", "status": "available", "path": "media/movie.mp4", "poster": "media/poster.jpg"},
            {"kind": "image", "status": "missing", "reason": "image_key_unavailable"},
            {"kind": "file", "status": "available", "path": "https://example.invalid/file"},
        ]
        count, text = _render([_message(sender=attack, content=attack, media=media)], {"target": attack, "note": attack})
        page = _Page(text)
        rows, info = json.loads(page.scripts["conversation-data"]), json.loads(page.scripts["viewer-info"])
        self.assertEqual(count, 1)
        self.assertNotIn(attack, text)
        self.assertEqual(info["title"], attack)
        self.assertEqual(rows[0][2:5], [attack, False, attack])
        self.assertEqual(rows[0][6][0]["original_path"], "media/original.wxgf")
        self.assertTrue(rows[0][6][0]["thumbnail"])
        self.assertEqual(rows[0][6][1]["poster"], "media/poster.jpg")
        self.assertIn("暂未取得", rows[0][6][2]["reason"])
        self.assertEqual(rows[0][6][3]["status"], "missing")
        for directive in ("default-src 'none'", "connect-src 'none'", "object-src 'none'", "form-action 'none'", "img-src file: data:", "media-src file:"):
            self.assertIn(directive, page.csp)
        self.assertNotIn("https:", page.csp)
        self.assertNotIn("innerHTML", viewer._SCRIPT)
        self.assertNotIn("fetch(", viewer._SCRIPT)
        # The only actual HTML image is the empty lightbox, never user markup.
        self.assertEqual(sum(tag == "img" for tag, _ in page.tags), 1)
        for path in (".", "..", "../a", "/a", "//server/a", "C:/a", "file:///a", "media//a", "media/./a", "a\x00b"):
            self.assertEqual(viewer._safe_path(path), "", path)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the independent synthetic DOM trial")
    def test_100000_messages_use_bounded_dom_and_filters_work(self):
        messages = [_message(content=f"Synthetic {index}", is_self=index % 2 == 0,
            ts_iso=f"2026-01-0{1 if index < 50000 else 2}T12:00:00+08:00") for index in range(100000)]
        messages[0]["media"] = [{"kind": "image", "status": "available", "path": "media/a #1.jpg",
            "original_path": "media/a.wxgf", "thumbnail": True, "reason": "仅找到缩略图"}]
        messages[1]["media"] = [{"kind": "audio", "status": "available", "path": "media/a.wav"}]
        messages[2]["media"] = [{"kind": "video", "status": "available", "path": "media/a.mp4", "poster": "media/a.jpg"}]
        messages[3]["media"] = [{"kind": "file", "status": "available", "path": "media/a.pdf"}]
        messages[4]["media"] = [{"kind": "image", "status": "missing", "reason": "image_key_unavailable"}]
        messages[-1]["content"] = "Needle </script><img src=x>"
        count, text = _render(messages)
        page = _Page(text)
        self.assertEqual(count, 100000)
        self.assertEqual(len(json.loads(page.scripts["conversation-data"])), 100000)
        mock = _MOCK_DOM.replace("FIXTURE_INFO", json.dumps(page.scripts["viewer-info"])).replace(
            "FIXTURE_ROWS", json.dumps(page.scripts["conversation-data"]))
        checks = r'''
(async()=>{
  assert(articles().length===150,'initial DOM must have only 150 messages');
  const nodes=registry.list.all();
  assert(nodes.some(node=>node.tagName==='audio'&&node.controls&&node.preload==='none'),'audio playback controls missing');
  assert(nodes.some(node=>node.tagName==='video'&&node.controls&&node.preload==='none'&&node.poster==='media/a.jpg'),'video or poster missing');
  assert(nodes.some(node=>node.tagName==='a'&&node.download!==undefined&&node.href==='media/a.pdf'),'file download missing');
  assert(nodes.some(node=>node.tagName==='a'&&node.href==='media/a.wxgf'),'original image download missing');
  assert(nodes.some(node=>node.textContent==='仅找到缩略图'),'thumbnail notice missing');
  assert(nodes.some(node=>(node.className||'')==='missing'),'missing attachment notice missing');
  const image=nodes.find(node=>node.tagName==='img');await image.fire('click');
  assert(registry.lightbox.open&&registry['large-image'].src==='media/a%20%231.jpg','image lightbox failed');
  await registry['close-lightbox'].fire('click');assert(!registry.lightbox.open,'lightbox close failed');
  await registry.next.fire('click');assert(articles().length===150&&Number(registry.page.value)===2,'next page failed');
  registry.size.value='200';await registry.size.fire('change');assert(articles().length===200,'page size failed');
  registry['jump-date'].value='2026-01-02';await registry.jump.fire('click');
  assert(Number(registry.page.value)===251,'date jump failed');
  registry.keyword.value='needle';await registry.filters.fire('submit');
  assert(articles().length===1&&registry.list.all().some(n=>n.textContent==='Needle </script><img src=x>'),'literal keyword filtering failed');
  registry.keyword.value='';registry.from.value='2026-01-02';registry.to.value='2026-01-02';await registry.filters.fire('submit');
  assert(articles().length===200&&registry.result.textContent.includes('50,000'),'date range filtering failed');
  registry.from.value='2026-01-03';await registry.filters.fire('submit');
  assert(registry.result.textContent.includes('开始日期不能晚于'),'invalid date range missing');
  console.log('100000-message synthetic DOM pagination/search/date/media/lightbox checks passed');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
'''
        process = subprocess.run([shutil.which("node")], input=mock + viewer._SCRIPT + checks,
            text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("checks passed", process.stdout)


if __name__ == "__main__":
    unittest.main()
