export const meta = {
  name: 'oracle-patterns',
  description: 'Audit god classes, import cycles, leaky abstractions and idiom clusters, then adversarially verify',
  whenToUse: 'After `afd-oracle ingest`; args {repo, packet_ids, batch_size?, run_id?, votes_high?}',
  phases: [
    { title: 'Analyze', detail: 'sonnet: 1 agent per batch of pattern packets', model: 'sonnet' },
    { title: 'Verify', detail: 'refuter per proposed insight: opus for medium/high (2 for high), sonnet for info/low', model: 'opus' },
  ],
}

const repo = args && args.repo
const ids = (args && args.packet_ids) || []
const B = (args && args.batch_size) || 5
const RUN = (args && args.run_id) || 'oracle-patterns'
const VOTES_HIGH = (args && args.votes_high) || 2
if (!ids.length) {
  log('No packet ids passed; nothing to do. The slash command fetches them with get_work_packets(ids_only=true).')
  return { proposed: 0, verified: [], rejected: [], unverified: [], skipped: [] }
}
const batches = []
for (let i = 0; i < ids.length; i += B) batches.push(ids.slice(i, i + B))
log(`${ids.length} pattern packets in ${batches.length} batches of <= ${B} (repo=${repo || 'default'}, run=${RUN})`)

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

const REFUTE = 'Refute if: the metrics in the claim do not match the graph (check with run_cypher); a "god class" is ' +
  'a cohesive data/record type or generated code; a cycle is type-only or guarded (TYPE_CHECKING) and harmless; ' +
  'a "leak" is a sanctioned friend/test access; an "idiom" cluster is boilerplate with no shared intent; or the ' +
  'cited commits did not introduce or grow the structure.'

const verify = async (ins) => {
  if (ins.duplicate) return { insight_id: ins.insight_id, status: 'duplicate' }
  const n = ins.severity === 'high' ? VOTES_HIGH : 1
  const deciding = n === 1
  // Opus only where a wrong verdict costs something; info/low findings get a Sonnet skeptic.
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
    log(`verifier failed for ${ins.insight_id}; left as proposed (sweep with /oracle-verify)`)
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
    `Fetch work packets ${JSON.stringify(batch)} with get_work_packets(kind="pattern", ids=<those ids>` +
    (repo ? `, repo="${repo}"` : '') + '). Packets are deterministic candidates: god_class (metrics + members), ' +
    'cycle (import strongly-connected component), leaky_abstraction (private/internal symbols used from outside), ' +
    'idiom_cluster (near-duplicate functions across directories). Confirm or dismiss each. Use run_cypher or ' +
    'get_symbol_history to find the commits that introduced or grew the structure. For confirmed smells call ' +
    'upsert_insight(label="AntiPattern", kind = "god_class" | "cycle" | "leaky_abstraction", ...); for a ' +
    'recurring, deliberate design idiom call upsert_insight(label="Pattern", kind="idiom", ...) naming the idiom; ' +
    'for an idiom cluster that is copy-paste drift use label="AntiPattern", kind="duplicated_logic". Cite the ' +
    'introducing/growing commits (role "introduced" or "evidence"), files with at_commit, and symbol uids; pass ' +
    `workflow_run="${RUN}", model="sonnet"` + (repo ? `, repo="${repo}"` : '') + '. Dismiss benign candidates in skipped.',
    { phase: 'Analyze', label: `analyze:batch${i + 1}`, agentType: 'oracle-analyst', model: 'sonnet',
      effort: 'medium', schema: PROPOSED }),
  (r) => (r ? parallel(r.insights.map((ins) => () => verify(ins))).then((v) => ({ v, skipped: r.skipped })) : null),
)

const flat = results.filter(Boolean).flatMap((x) => x.v).filter(Boolean)
const skipped = results.filter(Boolean).flatMap((x) => x.skipped)
const pick = (s) => flat.filter((x) => x.status === s).map((x) => x.insight_id)
const failedBatches = results.filter((r) => r === null).length
if (failedBatches) log(`${failedBatches} analysis batch(es) failed; their packets stay claimed (afd-oracle packets --reopen)`)
log(`proposed ${flat.length}: verified ${pick('verified').length}, rejected ${pick('rejected').length}, ` +
  `unverified ${pick('unverified').length}, duplicates ${pick('duplicate').length}; skipped ${skipped.length} packets`)
return { proposed: flat.length, verified: pick('verified'), rejected: pick('rejected'),
  unverified: pick('unverified'), duplicates: pick('duplicate'), skipped, failed_batches: failedBatches }
