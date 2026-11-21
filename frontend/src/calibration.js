import {calibrationStates, segments, store, supervisor} from './store.js';
import {blank, esc, pill, portDate, title} from './ui.js';

const pct = value => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const ratio = (missed, total) => total ? pct(missed / total) : '无优先案';
const band = value => value == null ? '—' : Number(value).toFixed(2);

function splitSamples(record, segment) {
  return record.samples.filter(sample => sample.segment === segment);
}

function flags(sample, current, candidate) {
  const marks = [];
  if (sample.flags.baseline_referral) marks.push('现行送审');
  if (sample.flags.baseline_missed) marks.push('现行漏放');
  if (sample.flags.candidate_referral) marks.push(`盲测送审 ${band(candidate)}`);
  if (sample.flags.candidate_missed) marks.push(`盲测漏放 ${band(candidate)}`);
  return marks.join(' · ') || '两侧均直接放行';
}

function sampleTable(record, segment) {
  const rows = splitSamples(record, segment);
  if (!rows.length) return '';
  const candidate = record.candidate_band;
  return `<div class="calibration-sample"><h4>${esc(segments[segment])} · ${rows.length} 件</h4>
  <div class="table-wrap"><table><thead><tr><th>案件</th><th>观测时间</th><th>f(x)</th><th>实际</th><th>判定</th></tr></thead><tbody>${rows.map(sample =>
    `<tr><td class="mono">${esc(sample.incident_code)}</td><td>${esc(portDate(sample.observed_at))}</td>
    <td class="mono">${sample.score >= 0 ? '+' : '−'}${Math.abs(sample.score).toFixed(4)}</td>
    <td>${sample.actual_priority ? '优先' : '常规'}</td><td>${esc(flags(sample, record.baseline_band, candidate))}</td></tr>`).join('')}
  </tbody></table></div></div>`;
}

function evaluationRow(record, activeBand) {
  const passed = record.state === 'passed';
  const canActivate = supervisor() && passed && record.candidate_band != null && record.candidate_band !== activeBand;
  const actions = [];
  if (canActivate) actions.push(`<form data-action="activateBand" data-code="${esc(record.code)}" class="operation"><button class="primary" type="submit">启用 ${band(record.candidate_band)}</button><span class="form-error" role="alert"></span></form>`);
  if (supervisor() && record.state === 'error') actions.push(`<form data-action="retryCalibration" data-code="${esc(record.code)}" class="operation"><button class="primary" type="submit">沿用原样本重试</button><span class="form-error" role="alert"></span></form>`);
  return `<article class="calibration-record">
    <header class="subhead"><div><div class="reference mono">${esc(record.code)} / ${esc(portDate(record.created_at))}</div>
      <h3>现行 ${band(record.baseline_band)} → 候选 ${band(record.candidate_band)}</h3></div>
      ${pill(record.state, calibrationStates)}</header>
    <dl class="measurements calibration-metrics">
      <div><dt>分段时刻</dt><dd>${esc(portDate(record.split_at))}</dd></div>
      <div><dt>前段 / 后段</dt><dd>${record.selection_rows} / ${record.blind_rows} 件</dd></div>
      <div><dt>后段送审 现行 / 候选</dt><dd>${record.current_referrals ?? '—'} / ${record.candidate_referrals ?? '—'}</dd></div>
      <div><dt>后段漏放 现行 / 候选</dt><dd>${record.current_missed ?? '—'}（${ratio(record.current_missed, record.blind_priority)}） / ${record.candidate_missed ?? '—'}（${ratio(record.candidate_missed, record.blind_priority)}）</dd></div>
      <div><dt>样本截止</dt><dd>${esc(portDate(record.sample_cutoff))}</dd></div>
      <div><dt>生成人 / 尝试</dt><dd>${esc(record.created_by)} · ${record.attempts}</dd></div>
      ${record.activated_at ? `<div><dt>启用时刻</dt><dd>${esc(portDate(record.activated_at))}</dd></div>
        <div><dt>启用后现行带宽</dt><dd>±${band(activeBand)}</dd></div>` : ''}
    </dl>
    <p class="model-note">${esc(record.verdict || '')}</p>
    ${record.error ? `<p class="warning">${esc(record.error)}</p>` : ''}
    ${actions.join('')}
    ${sampleTable(record, 'selection')}${sampleTable(record, 'blind')}
  </article>`;
}

export function calibration() {
  const data = store.calibration;
  if (!data) return title('间隔带校准', '读取校准记录。') + blank('正在读取', '等待校准接口返回。');
  const activeBand = data.active_band;
  const records = data.evaluations;
  return `${title('间隔带校准', '前段选带宽，后段盲测。盲测少送审且不漏放更多优先案才可启用。', `<span class="mono active-band">现行带宽 ±${band(activeBand)}</span>`)}
  ${supervisor() ? `<div class="calibration-actions"><form data-action="calibrate" class="operation"><button class="primary" type="submit">按当前已标注案件生成评估</button><span class="form-error" role="alert"></span></form>
    <p class="model-note">只使用结案时已标注的案件；新登记案件按现行带宽，已登记案件保留原带宽。</p></div>` : ''}
  <div class="calibration-list">${records.length ? records.map(record => evaluationRow(record, activeBand)).join('') : blank('尚无校准评估', '结案标注后，主管可生成第一份评估。')}</div>`;
}
