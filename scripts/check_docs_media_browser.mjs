/** Browser acceptance check for published docs media. Start a site preview and an
 * isolated app from this checkout first. Arguments: site URL (including base),
 * app origin. Checks real media decoding/playback; no mocked responses.
 *   node scripts/check_docs_media_browser.mjs http://localhost:4188/protoAgent/ http://localhost:7898
 */
import { chromium, webkit } from '@playwright/test';
import fs from 'node:fs';
const [site, app] = process.argv.slice(2);
if (!site || !app) throw Error('Provide the website URL including its base, and the app origin.');
const registry = JSON.parse(fs.readFileSync(new URL('../docs/dev/media/registry.json', import.meta.url)));
for (const [name, engine] of [['Chromium', chromium], ['WebKit', webkit]]) {
  const browser = await engine.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    for (const surface of ['website', 'reader']) {
      await page.goto(surface === 'website' ? new URL('tutorials/first-agent.html', site).href : `${app}/plugins/docs/view`);
      if (surface === 'reader') await page.locator('[data-path="tutorials/first-agent.md"]').click();
      const image = page.locator(surface === 'website' ? '.vp-doc img[data-docs-zoom]' : '#reader img[data-docs-zoom]').first();
      await image.waitFor();
      await image.focus();
      await page.keyboard.press('Enter');
      await page.locator('dialog[open]').waitFor();
      await page.getByRole('button', { name: 'Actual size', exact: true }).click();
      await page.keyboard.press('Escape');
      if (await page.locator('dialog[open]').count()) throw Error(`${surface}: Escape failed`);
      if (await image.evaluate(el => document.activeElement !== el)) throw Error(`${surface}: focus not restored`);
      await image.click();
      await page.getByRole('button', { name: 'Close', exact: true }).click();
      if (context.pages().length !== 1) throw Error(`${surface}: image opened another tab`);
      for (const [id, media] of Object.entries(registry.media)) {
        if (!['ready', 'refresh-needed'].includes(media.status)) continue;
        const assetUrl = file => surface === 'website' ? new URL(`media/${file}`, site).href : `${app}/plugins/docs/media/${file}`;
        for (const file of [media.asset, media.poster, media.transcript, media.captions].filter(Boolean)) {
          const response = await context.request.get(assetUrl(file));
          if (!response.ok()) throw Error(`${surface}: ${file}: HTTP ${response.status()}`);
        }
        if (['image', 'diagram'].includes(media.kind)) {
          await page.evaluate(async src => {
            const image = new Image(); image.src = src;
            await Promise.race([image.decode(), new Promise((_, reject) => setTimeout(() => reject(Error('Image decode timeout')), 15000))]);
            if (!image.naturalWidth) throw Error('Image has no decoded width');
          }, assetUrl(media.asset));
        }
        if (media.kind === 'video') {
          const result = await page.evaluate(async src => {
            const video = document.createElement('video');
            video.src = src; video.muted = true; video.playsInline = true; video.controls = true;
            document.body.append(video);
            try {
              await Promise.race([video.play(), new Promise((_, reject) => setTimeout(() => reject(Error('Playback timeout')), 15000))]);
              await new Promise(resolve => setTimeout(resolve, 300));
              return { width: video.videoWidth, time: video.currentTime };
            } finally { video.pause(); video.remove(); }
          }, assetUrl(media.asset));
          if (!result.width || !result.time) throw Error(`${surface}: ${id}: no decoded playback`);
        }
      }
      console.log(`${name} ${surface}: assets, video playback, same-page zoom, keyboard and focus passed`);
    }
  } finally { await browser.close(); }
}
