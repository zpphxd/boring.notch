// Frame-exact renderer: node launch/render.mjs [--preview 2,10,25] [--scale 2] [--out bruno-launch.mp4]
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, join, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require("playwright")); } catch { ({ chromium } = require("/opt/node-tools/node_modules/playwright")); }

const here = dirname(fileURLToPath(import.meta.url));
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, arr) => {
  if (a.startsWith("--")) acc.push([a.slice(2), arr[i + 1] && !arr[i + 1].startsWith("--") ? arr[i + 1] : true]);
  return acc;
}, []));
const scale = Number(args.scale || 1);
const types = { ".html": "text/html", ".json": "application/json", ".js": "text/javascript", ".css": "text/css" };

const server = createServer(async (req, res) => {
  try {
    const body = await readFile(join(here, decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "") || "video.html"));
    res.writeHead(200, { "content-type": types[extname(req.url.split("?")[0])] || "application/octet-stream" }); res.end(body);
  } catch { res.writeHead(404); res.end(); }
}).listen(0);
const port = server.address().port;

const browser = await chromium.launch({ args: ["--font-render-hinting=none", "--disable-lcd-text"] });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: scale });
await page.goto(`http://localhost:${port}/video.html`);
await page.evaluate(() => window.ready);
const tl = JSON.parse(await readFile(join(here, "timeline.json"), "utf8"));

if (args.preview) {
  for (const t of String(args.preview).split(",").map(Number)) {
    await page.evaluate(t => window.render(t), t);
    await page.screenshot({ path: join(args.dir || here, `preview-${t}.png`) });
    console.log("preview", t);
  }
} else {
  const fps = Number(args.fps || tl.fps), total = Math.round(tl.duration * fps);
  const out = args.out || join(here, "bruno-launch-silent.mp4");
  const ff = spawn("ffmpeg", ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(fps), "-i", "-",
    "-vf", `scale=1920:1080:flags=lanczos`, "-c:v", "libx264", "-preset", "slow", "-crf", "15", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
    { stdio: ["pipe", "inherit", "inherit"] });
  const start = Date.now();
  for (let f = 0; f < total; f++) {
    await page.evaluate(t => window.render(t), f / fps);
    const buf = await page.screenshot({ type: "png" });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once("drain", r));
    if (f % 150 === 0) console.log(`frame ${f}/${total}  ${((Date.now() - start) / 1000).toFixed(0)}s`);
  }
  ff.stdin.end();
  await new Promise(r => ff.on("close", r));
  console.log("wrote", out);
}
await browser.close();
server.close();
