export const meta = {
  name: 'oracle-catalog',
  description: 'Judge candidate-catalogue packets (about 190 kinds; each packet carries its own question), then adversarially verify',
  whenToUse: 'After `afd-oracle ingest`; args {repo, packet_ids, batch_size?, run_id?, votes_high?}',
  phases: [
    { title: 'Analyze', detail: 'sonnet: 1 agent per batch of catalogue packets', model: 'sonnet' },
    { title: 'Verify', detail: 'refuter per proposed insight: opus for medium/high (2 for high), sonnet for info/low', model: 'opus' },
  ],
}

const repo = args && args.repo
const ids = (args && args.packet_ids) || []
const B = (args && args.batch_size) || 5
const RUN = (args && args.run_id) || 'oracle-catalog'
const VOTES_HIGH = (args && args.votes_high) || 2
if (!ids.length) {
  log('No packet ids passed; nothing to do. Fetch them with get_work_packets(kind="catalog", ids_only=true).')
  return { proposed: 0, verified: [], rejected: [], unverified: [], skipped: [] }
}
const batches = []
for (let i = 0; i < ids.length; i += B) batches.push(ids.slice(i, i + B))
log(`${ids.length} catalogue packets in ${batches.length} batches of <= ${B} (repo=${repo || 'default'}, run=${RUN})`)

const PROPOSED = {
  type: 'object',
  properties: {
    insights: { type: 'array', items: { type: 'object',
      properties: { insight_id: { type: 'string' }, severity: { type: 'string' }, duplicate: { type: 'boolean' } },
      required: ['insight_id', 'severity'] } },
    skipped: { type: 'array', items: { type: 'object',
      properties: { packet_id: { type: 'string' }, reason: { type: 'string' } }, required: ['packet_id', 'reason'] } },
  },
  required: ['insights', 'skipped'],
}
const VERDICT = { type: 'object', properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['refuted', 'reason'] }

const REFUTE = 'Each insight came from a catalogue packet whose kind names a specific pitfall. Refute if: the cited ' +
  'commits do not show what the summary claims; any of the packet kind\'s dismissal conditions apply (they are in ' +
  'the packet as "dismiss_if"; fetch it with get_work_packets(kind="catalog", ids=[...], claim=false) if the ' +
  'insight names its packet, or re-derive from the citations); the risk is already handled at HEAD (check the ' +
  'current code with get_symbol_history / run_cypher); or the claim generalises beyond its evidence.'

const verify = async (ins) => {
  if (ins.duplicate) return { insight_id: ins.insight_id, status: 'duplicate' }
  const n = ins.severity === 'high' ? VOTES_HIGH : 1
  const deciding = n === 1
  const vModel = ins.severity === 'medium' || ins.severity === 'high' ? 'opus' : 'sonnet'
  const votes = (await parallel(Array.from({ length: n }, (_, k) => () => agent(
    `You are skeptic #${k + 1} of ${n}. Try to REFUTE proposed Insight ${ins.insight_id}` +
    (repo ? ` (repo "${repo}")` : '') + '. Load it, run verify_citations, then independently re-derive the claim ' +
    `with get_commit_diff on EVERY cited commit and get_symbol_history on cited symbols. ${REFUTE} ` +
    'Default refuted=true if uncertain. ' +
    (deciding
      ? `You are the deciding verifier: finish by calling set_insight_status(insight_id="${ins.insight_id}", ` +
        'status = "verified" if not refuted else "rejected", verdict_notes = your reason, ' +
        `votes = [{"model": "${vModel}", "refuted": <bool>, "reason": <reason>}]).`
      : 'Do NOT call set_insight_status; only return your verdict.'),
    { phase: 'Verify', label: `verify:${ins.insight_id.slice(-6)}#${k + 1}`, agentType: 'oracle-verifier',
      model: vModel, effort: 'medium', schema: VERDICT },
  )))).filter(Boolean)
  if (votes.length < n) {
    log(`verifier failed for ${ins.insight_id}; left as proposed (sweep with oracle-verify)`)
    return { insight_id: ins.insight_id, status: 'unverified' }
  }
  const survives = votes.every((v) => !v.refuted)
  if (!deciding) {
    await agent(
      `Call set_insight_status(insight_id="${ins.insight_id}", status="${survives ? 'verified' : 'rejected'}", ` +
      `verdict_notes=${JSON.stringify(votes.map((v) => v.reason).join(' | ').slice(0, 3500))}, ` +
      `votes=${JSON.stringify(votes.map((v) => ({ model: vModel, refuted: v.refuted, reason: v.reason.slice(0, 500) })))}` +
      '). Do not evaluate the insight yourself. Return "ok".',
      { phase: 'Verify', label: `record:${ins.insight_id.slice(-6)}`, agentType: 'oracle-verifier', model: 'sonnet',
        effort: 'low' })
  }
  return { insight_id: ins.insight_id, status: survives ? 'verified' : 'rejected', votes }
}

const results = await pipeline(
  batches,
  (batch, _, i) => agent(
    `Fetch work packets ${JSON.stringify(batch)} with get_work_packets(kind="catalog", ids=<those ids>` +
    (repo ? `, repo="${repo}"` : '') + '). Each packet is one deterministic candidate from the catalogue. It ' +
    'names the pitfall ("candidate", "pitfall"), states the exact judgement to make ("question"), lists when to ' +
    'dismiss it ("dismiss_if"), and carries the evidence (title, evidence, commit briefs, trimmed diffs, paths, ' +
    'symbol uids). For each packet: answer its question from the evidence (call get_commit_diff or ' +
    'get_symbol_history only when the trimmed evidence is insufficient). If the pitfall is real and actionable, ' +
    'call upsert_insight(label=<packet.insight_label>, kind=<packet.insight_kind>, ...): the title states the ' +
    'concrete finding, the summary answers the packet question and says what a developer changing this code must ' +
    'do or avoid. Cite commits (role "introduced", "fixed", "reverted" or "evidence"), files with at_commit and ' +
    `symbol uids copied from the packet; pass workflow_run="${RUN}", model="sonnet"` + (repo ? `, repo="${repo}"` : '') +
    '. If any dismiss_if condition applies or the evidence does not support a finding, put the packet in skipped ' +
    'with the reason. Skipping is a good outcome; a wrong insight costs far more than a missing one.',
    { phase: 'Analyze', label: `analyze:batch${i + 1}`, agentType: 'oracle-analyst', model: 'sonnet',
      effort: 'medium', schema: PROPOSED }),
  (r) => (r ? parallel(r.insights.map((ins) => () => verify(ins))).then((v) => ({ v, skipped: r.skipped })) : null),
)

const flat = results.filter(Boolean).flatMap((x) => x.v).filter(Boolean)
const skipped = results.filter(Boolean).flatMap((x) => x.skipped)
const pick = (s) => flat.filter((x) => x.status === s).map((x) => x.insight_id)
const failedBatches = results.filter((r) => r === null).length
if (failedBatches) log(`${failedBatches} analysis batch(es) failed; their packets stay claimed (release them from the dashboard)`)
log(`proposed ${flat.length}: verified ${pick('verified').length}, rejected ${pick('rejected').length}, ` +
  `unverified ${pick('unverified').length}, duplicates ${pick('duplicate').length}; skipped ${skipped.length} packets`)
return { proposed: flat.length, verified: pick('verified'), rejected: pick('rejected'),
  unverified: pick('unverified'), duplicates: pick('duplicate'), skipped, failed_batches: failedBatches }
