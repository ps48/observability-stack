import { runCreateWizard } from '../interactive.mjs';
import { applySimpleDefaults, validateConfig, fillDryRunPlaceholders } from '../cli.mjs';
import { renderPipeline } from '../render.mjs';
import { executePipeline } from '../main.mjs';
import { printError, printStep, theme, GoBack, eConfirm } from '../ui.mjs';

// ── Architecture diagram ────────────────────────────────────────────────────

function renderArchitectureDiagram(cfg) {
  const osLabel = cfg.serverless ? 'OpenSearch Serverless' : 'OpenSearch';
  const pathLabel = `/${cfg.pipelineName}/v1/*`;
  // Dynamically size the top box to fit the path
  const topInner = Math.max(pathLabel.length + 2, 21);
  const topPad = topInner - pathLabel.length;
  const leftPad = Math.floor(topPad / 2);
  const rightPad = topPad - leftPad;
  const midOffset = Math.floor(topInner / 2);

  const m = theme.muted;
  const a = theme.accent;
  const p = theme.primary;
  const h = theme.highlight;

  const lines = [];
  lines.push('');
  lines.push(`        ${m('┌' + '─'.repeat(topInner) + '┐')}`);
  lines.push(`        ${m('│')}${' '.repeat(leftPad)}${a('OTLP Endpoint')}${' '.repeat(Math.max(0, topInner - leftPad - 13))}${m('│')}`);
  lines.push(`        ${m('│')}${' '.repeat(leftPad)}${m(pathLabel)}${' '.repeat(rightPad)}${m('│')}`);
  lines.push(`        ${m('└' + '─'.repeat(midOffset) + '┬' + '─'.repeat(topInner - midOffset - 1) + '┘')}`);
  lines.push(`   ${m('┌' + '─'.repeat(12) + '┼' + '─'.repeat(12) + '┐')}`);
  lines.push(`   ${m('▼')}${' '.repeat(12)}${m('▼')}${' '.repeat(12)}${m('▼')}`);
  lines.push(`${m('┌─────────┐')}  ${m('┌─────────┐')}  ${m('┌─────────┐')}`);
  lines.push(`${m('│')} ${h('Logs')}    ${m('│')}  ${m('│')} ${h('Traces')}  ${m('│')}  ${m('│')} ${h('Metrics')} ${m('│')}`);
  lines.push(`${m('└────┬────┘')}  ${m('└────┬────┘')}  ${m('└────┬────┘')}`);
  lines.push(`     ${m('│')}       ${m('┌────┴────┐')}       ${m('│')}`);
  lines.push(`     ${m('│')}       ${m('▼')}         ${m('▼')}       ${m('│')}`);
  lines.push(`     ${m('│')}  ${m('┌────────┐ ┌────────┐')} ${m('│')}`);
  lines.push(`     ${m('│')}  ${m('│')} ${m('Raw')}    ${m('│ │')} ${m('Service')} ${m('│')} ${m('│')}`);
  lines.push(`     ${m('│')}  ${m('│')} ${m('Traces')} ${m('│ │')} ${m('Map')}     ${m('│')} ${m('│')}`);
  lines.push(`     ${m('│')}  ${m('└───┬────┘ └───┬────┘')} ${m('│')}`);
  lines.push(`     ${m('│')}      ${m('│')}       ${m('┌─┴──┐')}   ${m('│')}`);
  lines.push(`     ${m('▼')}      ${m('▼')}       ${m('▼')}    ${m('▼')}   ${m('▼')}`);
  lines.push(`${m('┌──────────────────────┐ ┌──────────────────┐')}`);
  lines.push(`${m('│')} ${p(osLabel.padEnd(21))}${m('│ │')} ${p('Prometheus')}       ${m('│')}`);
  lines.push(`${m('│')} ${m('logs, traces, svc-map')} ${m('│ │')} ${m('metrics, svc-map')} ${m('│')}`);
  lines.push(`${m('└──────────────────────┘ └──────────────────┘')}`);
  lines.push('');

  return lines;
}

export async function runCreate(session) {
  console.error();
  const cfg = await runCreateWizard(session);
  if (cfg === GoBack) return GoBack;

  // Apply simple-mode defaults
  if (!cfg.mode) cfg.mode = 'simple';
  if (cfg.mode === 'simple') applySimpleDefaults(cfg);

  // Validate
  const errors = validateConfig(cfg);
  if (errors.length) {
    for (const e of errors) printError(e);
    return;
  }

  // ── Show architecture diagram ──────────────────────────────────────────
  printStep('Architecture');
  const diagram = renderArchitectureDiagram(cfg);
  for (const line of diagram) console.error(`  ${line}`);

  // ── Show pipeline config preview ───────────────────────────────────────
  printStep('Pipeline config preview');
  console.error();

  // Render with placeholder endpoints for preview (don't mutate the real cfg)
  const previewCfg = { ...cfg };
  fillDryRunPlaceholders(previewCfg);
  const yaml = renderPipeline(previewCfg);

  for (const line of yaml.split('\n')) {
    if (line.trimStart().startsWith('#')) {
      console.error(`    ${theme.muted(line)}`);
    } else {
      const match = line.match(/^(\s*)([\w-]+)(:.*)/);
      if (match) {
        console.error(`    ${match[1]}${theme.accent(match[2])}${theme.muted(match[3])}`);
      } else {
        console.error(`    ${theme.muted(line)}`);
      }
    }
  }

  // ── Confirm to proceed ─────────────────────────────────────────────────
  console.error();
  const proceed = await eConfirm({
    message: 'Create these resources and deploy the stack?',
    default: true,
  });
  if (proceed === GoBack || !proceed) {
    console.error(`  ${theme.muted('Cancelled.')}`);
    console.error();
    return;
  }

  // Live path
  await executePipeline(cfg);
}
