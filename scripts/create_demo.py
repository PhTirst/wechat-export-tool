"""Create an entirely synthetic offline export, without opening any WeChat files."""
from __future__ import annotations
import argparse
import io
import json
import math
import struct
import wave
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from wechat_export_tool.render_media_viewer import render_viewer
from wechat_export_tool.security import private_directory, private_binary_writer, private_text_writer
from wechat_export_tool.verify_media_export import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('exports/demo'))
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.exists() and any(destination.iterdir()):
        parser.error('Output directory must be empty.')
    assets = private_directory(destination / 'attachments')
    buffer = io.BytesIO()
    Image.new('RGB', (480, 240), (24, 138, 98)).save(buffer, format='PNG')
    with private_binary_writer(assets / 'demo.png') as handle: handle.write(buffer.getvalue())
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
        handle.writeframes(b''.join(struct.pack('<h', int(900*math.sin(2*math.pi*440*i/24000))) for i in range(48000)))
    with private_binary_writer(assets / 'demo.wav') as handle: handle.write(audio.getvalue())
    with private_text_writer(assets / 'demo.txt') as handle: handle.write('Synthetic demonstration attachment.\n')
    start = datetime(2025, 1, 1, 12, tzinfo=timezone(timedelta(hours=8)))
    messages = []
    for index, (kind, text, media) in enumerate([
        (1, '这是合成演示数据，不包含真实聊天。', []),
        (3, '[图片]', [{'kind':'image','path':'attachments/demo.png','name':'demo.png','mime':'image/png','status':'available','reason':''}]),
        (34, '[语音 2s]', [{'kind':'audio','path':'attachments/demo.wav','name':'demo.wav','mime':'audio/wav','status':'available','reason':''}]),
        (49, '[文件] demo.txt', [{'kind':'file','path':'attachments/demo.txt','name':'demo.txt','mime':'text/plain','status':'available','reason':''}]),
        (49, '[文件] 缺失示例', [{'kind':'file','path':'','name':'missing.txt','mime':'','status':'missing','reason':'合成演示：附件未保存在本机'}]),
    ]):
        timestamp = start + timedelta(minutes=index)
        messages.append({'ts':timestamp.timestamp(),'ts_iso':timestamp.isoformat(),
            'sender':'我' if index % 2 else '演示联系人','sender_wxid':'demo-self' if index % 2 else 'demo-contact',
            'chat_room':'合成演示','conversation_id':'demo-conversation','content':text,'msg_type':kind,
            'is_self':bool(index%2),'is_group_chat':False,'server_id':str(1000+index),
            'local_id':index+1,'attachment_meta':None,'parse_status':'parsed','media':media})
    counts = Counter(f"{m['msg_type']}:{a['kind']}:{a['status']}" for m in messages for a in m['media'])
    missing = Counter(a['reason'] for m in messages for a in m['media'] if a['status']=='missing')
    summary = {'schema_version':1,'target':'合成演示','messages':len(messages),'raw_rows':len(messages),
        'media_references':sum(counts.values()),'counts':dict(counts),'missing_reasons':dict(missing),
        'note':'这些消息、图片与声音均由程序生成，仅用于演示。','status':'complete'}
    for name, data in [('messages.json',messages),('manifest.json',summary)]:
        with private_text_writer(destination/name) as handle: json.dump(data,handle,ensure_ascii=False,indent=2)
    with private_text_writer(destination/'raw_messages.jsonl') as handle:
        for message in messages: handle.write(json.dumps({'source_shard':'synthetic','conversation_id':'demo-conversation','row':message},ensure_ascii=False)+'\n')
    render_viewer(messages,destination/'index.html',summary)
    result = verify(destination,expect_count=5,expect_target='合成演示',deep=True)
    print(json.dumps({'demo':True,'verified':result['verified'],'messages':5},ensure_ascii=False))
    return 0 if result['verified'] else 1

if __name__=='__main__': raise SystemExit(main())
