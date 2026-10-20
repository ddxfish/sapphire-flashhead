// FlashHead viewer — one module behind the sidebar and the app page.
// The stream is packets of (header, PCM, JPEG frames) for the same 0.96 s;
// WebAudio's clock places both, so the mouth can't drift from the voice.
export const BASE = '/api/plugin/flashhead';
export const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

function createPlayer(canvas) {
    const g = canvas.getContext('2d');
    let ctx = null, abort = null, raf = 0, next = 0, frames = [], sources = [];

    function draw() {
        raf = requestAnimationFrame(draw);
        if (!ctx) return;
        const now = ctx.currentTime;
        let show = null;
        while (frames.length && frames[0].t <= now) {
            if (show) show.bmp.close();
            show = frames.shift();
        }
        if (show) {
            const px = Math.round(canvas.clientWidth * (window.devicePixelRatio || 1));
            if (px && canvas.width !== px) { canvas.width = canvas.height = px; g.imageSmoothingQuality = 'high'; }
            g.drawImage(show.bmp, 0, 0, canvas.width, canvas.height);
            show.bmp.close();
        }
    }

    function flush() {                  // she was stopped: cut what's scheduled
        const now = ctx.currentTime;
        sources.forEach(s => { try { s.stop(); } catch {} });
        sources = [];
        frames = frames.filter(f => (f.t <= now) || (f.bmp.close(), false));
        next = 0;
    }

    function schedule(head, bytes, off) {
        const dur = head.frames.length / head.fps;
        const t = Math.max(next, ctx.currentTime + 0.15);
        next = t + dur;
        if (head.audio) {
            const pcm = new Int16Array(bytes.slice(off, off + head.audio).buffer);
            const buf = ctx.createBuffer(1, pcm.length, head.sr);
            const ch = buf.getChannelData(0);
            for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
            const src = ctx.createBufferSource();
            src.buffer = buf;
            src.connect(ctx.destination);
            src.start(t);
            sources.push(src);
            src.onended = () => { sources = sources.filter(s => s !== src); };
        }
        let p = off + head.audio;
        if (document.hidden) return;        // a background tab: her voice plays on, frames nobody can see are not decoded
        head.frames.forEach((len, i) => {
            const blob = new Blob([bytes.subarray(p, p + len)], { type: 'image/jpeg' });
            p += len;
            createImageBitmap(blob).then(bmp => {
                if (!ctx) return bmp.close();      // stopped while it decoded
                frames.push({ t: t + i / head.fps, bmp });
                frames.sort((a, b) => a.t - b.t);
            });
        });
    }

    async function start() {
        ctx = new AudioContext();
        next = 0;
        abort = new AbortController();
        raf = requestAnimationFrame(draw);
        const r = await fetch(`${BASE}/stream`, { signal: abort.signal });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
        const reader = r.body.getReader();
        let buf = new Uint8Array(0);
        for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            const joined = new Uint8Array(buf.length + value.length);
            joined.set(buf); joined.set(value, buf.length);
            buf = joined;
            while (buf.length >= 4) {
                const hl = new DataView(buf.buffer, buf.byteOffset).getUint32(0);
                if (buf.length < 4 + hl) break;
                const head = JSON.parse(new TextDecoder().decode(buf.subarray(4, 4 + hl)));
                const media = head.frames ? head.audio + head.frames.reduce((a, b) => a + b, 0) : 0;
                const total = 4 + hl + media;
                if (buf.length < total) break;
                if (head.kind === 'flush') flush();
                else if (head.frames) schedule(head, buf, 4 + hl);
                buf = buf.slice(total);
            }
        }
    }

    function stop() {
        abort?.abort();
        cancelAnimationFrame(raf);
        frames.forEach(f => f.bmp.close());
        frames = [];
        sources = [];
        ctx?.close();
        ctx = null;
    }

    return { start, stop };
}

// Her settings tab (web/index.js): from Settings already, switch tabs; else enter Settings on it.
async function openSettings() {
    const { switchView, getCurrentView } = await import('/static/core/router.js');
    if (getCurrentView() === 'settings') {
        document.getElementById('view-settings')
            ?.dispatchEvent(new CustomEvent('settings-navigate', { detail: { tab: 'flashhead' }, bubbles: true }));
        return;
    }
    window._settingsTab = 'flashhead';
    switchView('settings');
}

export function mount(root, { big = false } = {}) {
    const size = big ? 'min(80vh, 90vw)' : '100%';
    root.innerHTML = `
        <div class="fh-wrap" style="display:flex;flex-direction:column;gap:var(--space-sm,8px);align-items:center;${big ? 'padding:24px;' : ''}">
            <div style="position:relative;width:${size};aspect-ratio:1;background:#000;border-radius:var(--radius-md,8px);overflow:hidden;">
                <img class="fh-still" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.5;">
                <canvas class="fh-canvas" width="512" height="512" style="position:absolute;inset:0;width:100%;height:100%;"></canvas>
            </div>
            <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:center;">
                <button class="fh-watch btn">▶ Watch</button>
                <button class="fh-retry btn" hidden title="Start the service again: a failed download resumes where it stopped">↻ Retry</button>
                <button class="fh-settings btn" title="Faces, moods, idle and screens">⚙</button>
            </div>
            <div class="fh-status" style="font-size:var(--font-sm,12px);color:var(--text-muted);text-align:center;"></div>
        </div>`;
    const $ = s => root.querySelector(s);
    const canvas = $('.fh-canvas'), still = $('.fh-still'), btn = $('.fh-watch'), status = $('.fh-status');
    const player = createPlayer(canvas);
    let watching = false, poll = 0, faceKey = '';

    async function tick() {
        if (document.hidden || !root.isConnected || !canvas.offsetParent) return;   // nobody sees it: no polling
        try {
            const s = await (await fetch(`${BASE}/status`)).json();
            const mood = s.mood ? ` · mood: ${s.mood}` : '';
            const stuck = ['error', 'unsupported', 'offline'].includes(s.status);
            $('.fh-retry').hidden = !stuck;
            status.textContent = stuck ? (s.error || s.status)       // the reason, in words, alone
                : !s.face_source ? 'No face yet: give her persona an avatar, or upload one in ⚙ › Faces'
                : s.note || s.warning
                || `${s.status} · face: ${s.face_source}${mood} · viewers: ${s.viewers ?? 0}`;
            const key = `${s.face_source}|${s.mood}|${s.face_changed || ''}`;
            if (key !== faceKey) {                     // a face changed in Settings: the still follows
                faceKey = key;
                still.src = `${BASE}/face?t=${Date.now()}`;
            }
        } catch { status.textContent = 'status unavailable'; }
    }
    tick();
    poll = setInterval(tick, 3000);

    async function watch() {
        watching = true;
        btn.textContent = '■ Stop';
        still.style.opacity = '0';
        try { await player.start(); }
        catch (e) { if (e.name !== 'AbortError') status.textContent = `stream error: ${e.message}`; }
        unwatch();
    }
    function unwatch() {
        if (!watching) return;
        watching = false;
        player.stop();
        btn.textContent = '▶ Watch';
        still.style.opacity = '.5';
        canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    }

    btn.onclick = () => (watching ? unwatch() : watch());
    $('.fh-settings').onclick = openSettings;
    $('.fh-retry').onclick = async () => {
        $('.fh-retry').hidden = true;
        status.textContent = 'starting again…';
        await fetch(`${BASE}/restart`, { method: 'POST', headers: { 'X-CSRF-Token': csrf() } }).catch(() => {});
        tick();
    };

    // Only while watching: a collapsed accordion or scrolled-away face stops the GPU.
    const seen = new IntersectionObserver(([e]) => { if (!e.isIntersecting) unwatch(); });
    seen.observe(canvas);

    return () => { unwatch(); clearInterval(poll); seen.disconnect(); };
}
