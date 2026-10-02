// T2（PR-2 改动 2 Layer C）XSS 净化测试 harness。
// 由 tests/test_xss.py 以 node 调起：从 app.js 提取 escapeFallback（纯正则兜底实现），
// 加载 vendor/marked.min.js，对每个注入向量走「markdown 渲染 → escapeFallback」
// 的完整链路，输出 JSON 结果数组（末尾附加反例：pre/code 不得误杀）。
// 用法：node tests/xss_harness.cjs <app.js 路径> <marked.min.js 路径>
const fs = require("fs");

const appJs = process.argv[2];
const markedJs = process.argv[3];
if (!appJs || !markedJs) {
  console.error("usage: node xss_harness.cjs <app.js> <marked.min.js>");
  process.exit(2);
}

const src = fs.readFileSync(appJs, "utf8");
const m = src.match(/function escapeFallback\(html\) \{[\s\S]*?\n\}/);
if (!m) {
  console.error("escapeFallback not found in app.js");
  process.exit(2);
}
eval(m[0]);

const mk = fs.readFileSync(markedJs, "utf8");
const marked = (function () {
  var module = { exports: {} };
  var exports = module.exports;
  eval(mk);
  return module.exports;
})();
if (typeof marked.parse !== "function") {
  console.error("marked.parse missing");
  process.exit(2);
}

const vectors = [
  "<svg onload=alert(1)>",
  '<math href="javascript:alert(1)">',
  "<details open ontoggle=alert(1)>",
  "<img src=x onerror=alert(1)>",
  "[click](javascript:alert(1))",
  '<iframe srcdoc="<img src=x onerror=alert(1)>">',
];

// 反例：合法代码块不得误杀——class 属性与引号必须保留
const benign = '<pre><code class="language-js">const a = 1</code></pre>';

const results = vectors.map((v) => {
  const html = marked.parse(v);
  return { vector: v, rendered: html, out: escapeFallback(html) };
});
const benignHtml = marked.parse(benign);
results.push({ vector: benign, rendered: benignHtml, out: escapeFallback(benignHtml) });

console.log(JSON.stringify(results));
