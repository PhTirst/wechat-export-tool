/* Offline SILK -> WAV batch. npm silk-wasm is an external runtime dependency. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { spawnSync } = require('node:child_process');

function waveBytes(pcm, rate = 24000) {
  if (!pcm.length || pcm.length % 2 || pcm.length > 0xffffffff - 36) throw new Error('pcm_invalid');
  const header = Buffer.alloc(44);
  header.write('RIFF', 0); header.writeUInt32LE(pcm.length + 36, 4);
  header.write('WAVEfmt ', 8); header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
  header.writeUInt32LE(rate, 24); header.writeUInt32LE(rate * 2, 28);
  header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34);
  header.write('data', 36); header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

function loadSilk(manifest) {
  const specifier = manifest.silkModule || 'silk-wasm';
  // Explicit absolute entries work regardless of cwd. Default module lookup
  // checks cwd's npm project and the installed/source package ancestry.
  for (const loader of [createRequire(path.join(process.cwd(), 'package.json')), createRequire(__filename)]) {
    try {
      const candidate = loader(specifier);
      if (candidate && typeof candidate.decode === 'function') return candidate;
    } catch (_) { /* Never print module paths or exception details. */ }
  }
  throw new Error('voice_decoder_missing');
}

async function main(manifestPath, resultPath) {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const results = {};
  let silk;
  try { silk = loadSilk(manifest); } catch (_) {
    for (const item of manifest.jobs) results[item.id] = { status: 'missing', reason: 'voice_decoder_missing' };
  }
  if (silk) for (const item of manifest.jobs) {
    let decoded;
    try {
      const result = await silk.decode(fs.readFileSync(item.input), 24000);
      const pcm = Buffer.from(result.data.buffer, result.data.byteOffset, result.data.byteLength);
      const durationMs = pcm.length / 48;
      if (item.expectedMs > 0 && Math.abs(durationMs - item.expectedMs) > Math.max(1000, item.expectedMs * 0.20)) {
        results[item.id] = { status: 'missing', reason: 'voice_duration_mismatch' };
        continue;
      }
      fs.writeFileSync(item.output, waveBytes(pcm), { mode: 0o600 });
      decoded = { status: 'available', duration_ms: durationMs };
      if (manifest.ffmpeg && item.mp3) {
        const conversion = spawnSync(manifest.ffmpeg, [
          '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', item.output,
          '-c:a', 'libmp3lame', '-b:a', '48k', item.mp3
        ], { stdio: 'ignore', windowsHide: true, timeout: 60000 });
        if (conversion.status === 0 && fs.existsSync(item.mp3) && fs.statSync(item.mp3).size > 0) decoded.mp3 = true;
      }
    } catch (_) {
      decoded = { status: 'missing', reason: 'silk_decode_failed' };
    }
    results[item.id] = decoded;
  }
  fs.writeFileSync(resultPath, JSON.stringify(results), { mode: 0o600 });
  console.log(JSON.stringify({ total: manifest.jobs.length,
    available: Object.values(results).filter(item => item.status === 'available').length }));
}

module.exports = { waveBytes, loadSilk };
if (require.main === module) main(process.argv[2], process.argv[3]).catch(() => {
  console.error('voice_batch_failed'); process.exitCode = 1;
});
