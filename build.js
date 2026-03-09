/**
 * Build script for Klein Meridian
 *
 * Usage: node build.js
 *
 * 1. Reads index.html and content.json
 * 2. Bakes saved text edits into the HTML
 * 3. Strips the editor UI (button, toast, editor JS/CSS)
 * 4. Outputs a clean production build to /docs (for GitHub Pages)
 */

const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const CONTENT_FILE = path.join(ROOT, 'content.json');
const SRC_HTML = path.join(ROOT, 'index.html');
const OUT_DIR = path.join(ROOT, 'docs');

// Read content overrides
let content = {};
try {
  content = JSON.parse(fs.readFileSync(CONTENT_FILE, 'utf8'));
} catch {
  console.log('No content.json found — using defaults from HTML.');
}

// Read source HTML
let html = fs.readFileSync(SRC_HTML, 'utf8');

// Bake content.json values into HTML
for (const [key, value] of Object.entries(content)) {
  // Match data-editable="key">...< and replace inner text
  const regex = new RegExp(
    `(data-editable="${key}"[^>]*>)(.*?)(</)`,
    'gs'
  );
  html = html.replace(regex, `$1${escapeHtml(value)}$3`);
}

// Remove editor UI elements
html = html.replace(/<!-- Editor UI -->[\s\S]*?<div class="edit-toast"[^>]*>.*?<\/div>/g, '');

// Remove editor CSS block
html = html.replace(/\/\* ── Inline Editor ──[\s\S]*?\.edit-toast\.show \{[^}]*\}\s*/g, '');

// Remove editor JS block
html = html.replace(/\/\/ ── Inline Editor ──[\s\S]*?\}\)\(\);\s*/g, '');

// Remove data-editable attributes (clean production markup)
html = html.replace(/ data-editable="[^"]*"/g, '');

// Clean up any double blank lines
html = html.replace(/\n{3,}/g, '\n\n');

// Create output directory
if (!fs.existsSync(OUT_DIR)) {
  fs.mkdirSync(OUT_DIR, { recursive: true });
}

// Copy images
const imgSrc = path.join(ROOT, 'images');
const imgDest = path.join(OUT_DIR, 'images');
if (!fs.existsSync(imgDest)) {
  fs.mkdirSync(imgDest, { recursive: true });
}
for (const file of fs.readdirSync(imgSrc)) {
  fs.copyFileSync(path.join(imgSrc, file), path.join(imgDest, file));
}

// Write production HTML
fs.writeFileSync(path.join(OUT_DIR, 'index.html'), html, 'utf8');

// Copy CNAME if it exists
const cname = path.join(ROOT, 'CNAME');
if (fs.existsSync(cname)) {
  fs.copyFileSync(cname, path.join(OUT_DIR, 'CNAME'));
}

console.log('✓ Build complete → /docs');
console.log(`  ${Object.keys(content).length} content override(s) applied`);

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
