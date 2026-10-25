/* Supersampled transparent surface for the native Windows bubble. */
(function () {
  'use strict';
  const ball = document.getElementById('ball');
  let queued = false, sending = false, dirty = false, hovered = false, lastFrame = '', timer = null;
  let sparkImage = null;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const api = () => window.pywebview?.api;

  function schedule() {
    dirty = true;
    if (queued || sending) return;
    queued = true;
    setTimeout(paint, 0);
  }
  async function paint() {
    queued = false;
    if (sending) { dirty = true; return; }
    if (!api()?.present_bubble_surface) return;
    sending = true; dirty = false;
    try {
      const ratio = Math.min(8, Math.max(2, devicePixelRatio * 2));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(innerWidth * ratio); canvas.height = Math.round(innerHeight * ratio);
      const ctx = canvas.getContext('2d');
      ctx.scale(ratio, ratio);
      const css = getComputedStyle(ball), radius = parseFloat(css.width) / 2;
      const x = innerWidth / 2, y = innerHeight / 2;
      const animated = ball.dataset.motion !== 'false' && !reduced.matches;
      const scale = hovered && animated ? 1.03 : 1;
      ctx.translate(x, y); ctx.scale(scale, scale);
      ctx.globalAlpha = parseFloat(css.opacity);
      ctx.shadowColor = '#101a3026'; ctx.shadowBlur = 4; ctx.shadowOffsetY = 1.5;
      ctx.beginPath(); ctx.arc(0, 0, radius - .65, 0, 2 * Math.PI);
      ctx.fillStyle = css.backgroundColor; ctx.fill();
      ctx.shadowColor = 'transparent'; ctx.lineWidth = .85;
      ctx.strokeStyle = css.borderTopColor; ctx.stroke();
      const image = ball.querySelector('img');
      if (image?.complete && image.naturalWidth) {
        ctx.save(); ctx.filter = getComputedStyle(image).filter;
        const cover = getComputedStyle(image).objectFit === 'cover';
        if (cover) { ctx.beginPath(); ctx.arc(0, 0, 12, 0, Math.PI * 2); ctx.clip(); }
        const fit = (cover ? Math.max : Math.min)(24 / image.naturalWidth, 24 / image.naturalHeight);
        const width = image.naturalWidth * fit, height = image.naturalHeight * fit;
        ctx.drawImage(image, -width / 2, -height / 2, width, height); ctx.restore();
      } else if (ball.querySelector('svg')) {
        if (!sparkImage) {
          const svg = ball.querySelector('svg').cloneNode(true);
          svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
          svg.setAttribute('color', css.color); svg.setAttribute('width', '24'); svg.setAttribute('height', '24');
          sparkImage = new Image();
          sparkImage.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(svg));
          await sparkImage.decode();
        }
        ctx.drawImage(sparkImage, -12, -12, 24, 24);
      }
      if (ball.classList.contains('running') || ball.classList.contains('completed') || ball.classList.contains('error')) {
        ctx.save();
        if (animated && ball.classList.contains('running')) ctx.globalAlpha *= .7 + .3 * Math.cos(performance.now() / 1500 * Math.PI * 2);
        const dot = getComputedStyle(ball.querySelector('.task-dot'));
        const pos = (radius - 3) / Math.sqrt(2);
        ctx.beginPath(); ctx.arc(pos, pos, 4, 0, Math.PI * 2);
        ctx.fillStyle = dot.backgroundColor; ctx.fill();
        ctx.lineWidth = 1.7; ctx.strokeStyle = css.backgroundColor; ctx.stroke(); ctx.restore();
      }
      const frame = canvas.toDataURL('image/png');
      if (frame !== lastFrame) {
        if (await api().present_bubble_surface(frame)) lastFrame = frame;
      }
      clearTimeout(timer);
      if (animated && ball.classList.contains('running')) timer = setTimeout(schedule, 100);
    } catch (error) {
      console.warn('Bubble surface unavailable; retaining the WebView bubble.', error);
    } finally {
      sending = false;
      if (dirty) schedule();
    }
  }
  window.novaMindBubbleHover = active => { hovered = active; return paint(); };
  window.novaMindRefreshBubble = async () => {
    await window.novaMindSyncTheme?.();
    await window.novaMindSyncBubble?.();
    return paint();
  };
  new MutationObserver(() => { sparkImage = null; schedule(); }).observe(ball, {attributes: true, childList: true, subtree: true});
  new MutationObserver(() => { sparkImage = null; schedule(); }).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
  ball.addEventListener('load', schedule, true);
  window.addEventListener('resize', schedule);
  window.addEventListener('pywebviewready', schedule);
  reduced.addEventListener('change', schedule);
  schedule();
})();
