#!/usr/bin/env node
/**
 * 把 MC 的两个 agent preset（陪玩 / 单人游玩）装进本机 DSH。
 *
 * 做法：读 templates/*.patch.yml.tpl → 替换成本机路径 → 幂等合并进
 *       $DSH_HOME/profiles/<profile>/cordis.patch.yml。
 *
 * 为什么要有这个脚本：preset 定义里必须写死 venv python、wrapper、skills 目录的
 * **绝对路径**，而这三样在不同机器上都不一样。让用户手工改三处路径再手工插入
 * YAML，既容易漏也容易把缩进改坏（我见过一次拼坏导致 DSH 启动直接崩）。所以
 * 这里统一探测、统一替换、统一校验。
 *
 * 用法：
 *   node plugins/dsh-mc-preset/install.mjs                 自动探测 + 装到 web profile
 *   node plugins/dsh-mc-preset/install.mjs --profile desktop
 *   node plugins/dsh-mc-preset/install.mjs --dry-run       只看会写什么，不落盘
 *   node plugins/dsh-mc-preset/install.mjs --uninstall     从 profile 里摘掉
 *   node plugins/dsh-mc-preset/install.mjs --python <路径> --wrapper <路径>
 *   node plugins/dsh-mc-preset/install.mjs --port 5557     改 bridge 端口
 *
 * 依赖：Node ≥ 22（用内置 node:fs / node:path，不需要装包）。YAML 校验会用
 *       profile 里已有的 js-yaml（DSH 自带），拿不到就跳过校验但会打印提示。
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');           // plugins/dsh-mc-preset → 仓库根
const MARK_BEGIN = '# === MC agent presets BEGIN ===';
const MARK_END = '# === MC agent presets END ===';

// ---------------------------------------------------------------- 参数

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const opt = (name, fallback = '') => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

const DRY_RUN = flag('--dry-run');
const UNINSTALL = flag('--uninstall');
const PROFILE_NAME = opt('--profile', 'web');
const PORT = opt('--port', '5556');
const FORCED_PYTHON = opt('--python');
const FORCED_WRAPPER = opt('--wrapper');

const say = (line = '') => console.log(line);
const ok = (line) => console.log(`  ✅ ${line}`);
const warn = (line) => console.log(`  ⚠️  ${line}`);
const bad = (line) => console.log(`  ❌ ${line}`);

// ---------------------------------------------------------------- 探测

function dshHome() {
  return process.env.DSH_HOME || path.join(process.env.USERPROFILE || process.env.HOME || '', '.dsh');
}

/** 仓库里的 venv python；没有就退回系统 python。 */
function findPython() {
  if (FORCED_PYTHON) return { path: FORCED_PYTHON, source: '--python' };
  const candidates = [
    path.join(REPO, '.venv', 'Scripts', 'python.exe'),
    path.join(REPO, '.venv', 'bin', 'python'),
    path.join(REPO, 'venv', 'Scripts', 'python.exe'),
    path.join(REPO, 'venv', 'bin', 'python'),
  ];
  for (const c of candidates) if (fs.existsSync(c)) return { path: c, source: '仓库 venv' };
  // 系统 python：只告诉用户要用哪个，不替他选
  return { path: '', source: '未找到' };
}

function findWrapper() {
  if (FORCED_WRAPPER) return { path: FORCED_WRAPPER, source: '--wrapper' };
  const candidates = [
    path.join(REPO, 'scripts', 'mcp_stdio_wrapper.py'),
    path.join(REPO, '工具', 'mcp_stdio_wrapper.py'),
  ];
  for (const c of candidates) if (fs.existsSync(c)) return { path: c, source: '仓库内' };
  return { path: '', source: '未找到' };
}

function findSkills() {
  const dir = path.join(HERE, 'skills');
  return fs.existsSync(dir) ? dir : '';
}

function profileDir() {
  return path.join(dshHome(), 'profiles', PROFILE_NAME);
}

// ---------------------------------------------------------------- 合并

/** 用 DSH 同一个 schema 校验；拿不到 js-yaml 就返回 null（跳过）。 */
async function tryYaml() {
  const tries = [
    path.join(profileDir(), 'node_modules', 'js-yaml', 'index.js'),
    path.join(dshHome(), 'node_modules', 'js-yaml', 'index.js'),
  ];
  for (const p of tries) {
    if (!fs.existsSync(p)) continue;
    try { return (await import('file://' + p.replaceAll('\\', '/'))).default; } catch { /* 换下一个 */ }
  }
  return null;
}

function stripBlock(text) {
  const b = text.indexOf(MARK_BEGIN);
  if (b < 0) return text;
  const e = text.indexOf(MARK_END);
  if (e < 0) return text;
  return text.slice(0, b) + text.slice(e + MARK_END.length);
}

async function main() {
  say('');
  say('MC agent preset 安装器');
  say('─'.repeat(56));

  const patchFile = path.join(profileDir(), 'cordis.patch.yml');

  // ---- 卸载
  if (UNINSTALL) {
    if (!fs.existsSync(patchFile)) { bad(`找不到 ${patchFile}`); process.exit(1); }
    const before = fs.readFileSync(patchFile, 'utf8');
    const after = stripBlock(before).replace(/\n{3,}$/, '\n');
    if (before === after) { warn('profile 里没有 MC 预设块，无需卸载'); return; }
    if (DRY_RUN) { ok('[dry-run] 会移除 MC 预设块'); return; }
    backup(patchFile);
    fs.writeFileSync(patchFile, after, 'utf8');
    ok('已从 profile 移除 MC 预设块（记得重启 DSH）');
    return;
  }

  // ---- 探测
  say('');
  say('【1/5】探测本机路径');
  const py = findPython();
  const wrapper = findWrapper();
  const skills = findSkills();

  py.path ? ok(`python : ${py.path}  (${py.source})`) : bad(`python : 未找到 —— 用 --python <路径> 指定`);
  wrapper.path ? ok(`wrapper: ${wrapper.path}  (${wrapper.source})`) : bad(`wrapper: 未找到 —— 用 --wrapper <路径> 指定`);
  skills ? ok(`skills : ${skills}`) : bad('skills : 缺失（本包应自带 skills/ 目录）');

  if (!py.path || !wrapper.path || !skills) {
    say('');
    bad('关键路径缺失，已中止（没有改动任何文件）');
    say('  提示：Python 环境按 DSH-READHERE §3.1 建好后再来跑本脚本。');
    process.exit(2);
  }

  // ---- 渲染
  say('');
  say('【2/5】渲染模板');
  const values = { __VENV_PYTHON__: py.path, __WRAPPER__: wrapper.path, __SKILLS_DIR__: skills };
  const blocks = [];
  for (const name of ['mc-companion', 'mc-solo']) {
    const tpl = path.join(HERE, 'templates', `${name}.patch.yml.tpl`);
    if (!fs.existsSync(tpl)) { bad(`缺少模板 ${tpl}`); process.exit(1); }
    let text = fs.readFileSync(tpl, 'utf8');
    for (const [k, v] of Object.entries(values)) text = text.split(k).join(v);
    // 端口可覆盖
    text = text.split('--port\n                - \'5556\'').join(`--port\n                - '${PORT}'`);
    text = text.split(`- '5556'`).join(`- '${PORT}'`);
    const left = text.match(/__[A-Z_]+__/g);
    if (left) { bad(`${name} 还有没替换的占位符：${[...new Set(left)].join(', ')}`); process.exit(1); }
    // 只保留从行首 "- insert:" 开始的部分（跳过文件头的注释）
    const m = text.match(/^- insert:/m);
    if (!m) { bad(`${name} 模板里找不到行首的 "- insert:"`); process.exit(1); }
    blocks.push(text.slice(m.index).trimEnd());
    ok(`${name} 已渲染`);
  }

  // ---- 合并
  say('');
  say('【3/5】合并进 profile');
  if (!fs.existsSync(patchFile)) {
    bad(`profile 不存在：${profileDir()}`);
    say('  先确认 profile 名字对不对（ls $DSH_HOME/profiles/）');
    process.exit(1);
  }
  const original = fs.readFileSync(patchFile, 'utf8');
  const base = stripBlock(original).trimEnd();
  const merged = base + '\n\n' + MARK_BEGIN + '\n' + blocks.join('\n\n') + '\n' + MARK_END + '\n';

  if (original.includes(MARK_BEGIN)) warn('profile 里已有 MC 预设块 → 会整体替换（幂等）');
  ok(`profile : ${patchFile}`);

  // ---- 校验
  say('');
  say('【4/5】校验 YAML');
  const yaml = await tryYaml();
  if (!yaml) {
    warn('拿不到 js-yaml，跳过结构校验（DSH 启动时会自己校验，出错会明确报行号）');
  } else {
    let doc;
    try { doc = yaml.load(merged, { schema: yaml.JSON_SCHEMA }); }
    catch (e) { bad('YAML 解析失败，已中止：' + e.message); process.exit(3); }
    if (!Array.isArray(doc)) { bad('顶层不是数组，已中止'); process.exit(3); }
    for (const [i, row] of doc.entries()) {
      if (typeof row !== 'object' || row === null || Array.isArray(row)) {
        bad(`第 ${i + 1} 行不是映射，已中止`); process.exit(3);
      }
      if ('insert' in row && !Array.isArray(row.insert)) {
        bad(`第 ${i + 1} 行的 insert 不是数组（typeof=${typeof row.insert}），已中止`); process.exit(3);
      }
    }
    const presets = doc.filter((r) => r.insert).flatMap((r) => r.insert)
      .filter((e) => e.name === '@deepseek-ai/dsh-agent-preset' && String(e.config?.id || '').startsWith('mc-'));
    ok(`结构合法：${doc.length} 行，其中 MC preset ${presets.length} 个（${presets.map((p) => p.config.id).join(', ')}）`);
    if (presets.length !== 2) { bad('预期的两个 MC preset 不齐，已中止'); process.exit(3); }
  }

  // ---- 落盘
  say('');
  say('【5/5】写入');
  if (DRY_RUN) {
    ok('[dry-run] 校验全部通过，未写入任何文件');
    say('');
    say('去掉 --dry-run 就会真正写入。');
    return;
  }
  backup(patchFile);
  fs.writeFileSync(patchFile, merged, 'utf8');
  ok(`已写入 ${patchFile}`);

  say('');
  say('─'.repeat(56));
  say('装好了。接下来：');
  say('  1. 重启 DSH');
  say('  2. 新建会话 → 预设选「MC 陪玩」或「MC 单人游玩」');
  say('  3. 没选预设的会话看不到 MC 工具（这是设计，不是 bug）');
  say('');
  say('想撤销：node plugins/dsh-mc-preset/install.mjs --uninstall');
}

function backup(file) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const dst = `${file}.bak-${stamp}-before-mc-preset`;
  fs.copyFileSync(file, dst);
  ok(`已备份 → ${path.basename(dst)}`);
}

main().catch((e) => { bad(String(e && e.stack ? e.stack : e)); process.exit(1); });
