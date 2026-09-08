"""Hash-bound June membership from the user-designated current-meter column."""
from __future__ import annotations

import io
import json
import re
from collections import Counter
from sales_monthly_categories import (
    verified_bytes, validate_identities, creator_eligible_ids,
    ingest_workbook, complete_exceptions,
)


def current_members(source, canonical_ids):
    """Membership is independent of categories, purchases and predecessor values."""
    import openpyxl
    aliases = {}
    for mid in canonical_ids:
        aliases.setdefault(mid.lstrip('0') or '0', []).append(mid)
    book = openpyxl.load_workbook(io.BytesIO(verified_bytes(source, 'June source')), read_only=True, data_only=True)
    try:
        stream = book[source['sheet']].iter_rows(values_only=True)
        headers = list(next(stream))
        if headers.count('MeterNumber') != 1:
            raise ValueError('Missing/duplicate MeterNumber header')
        index = headers.index('MeterNumber')
        records, invalid, ambiguous, unmatched = [], [], [], []
        for n, row in enumerate(stream, 2):
            if not any(v is not None and str(v).strip() for v in row):
                continue
            raw = row[index]
            text = str(raw).strip() if raw is not None else ''
            entry = {'sourceRow': n, 'sourceIdentity': text}
            if not re.fullmatch(r'[0-9]+', text) or not text.strip('0'):
                invalid.append(entry)
                continue
            matches = [text] if text in canonical_ids else aliases.get(text.lstrip('0'), [])
            if len(matches) != 1:
                (ambiguous if matches else unmatched).append(entry)
                continue
            records.append({**entry, 'canonicalId': matches[0]})
        counts = Counter(r['canonicalId'] for r in records)
        return {'identityHeader': 'MeterNumber', 'identityColumn': index + 1,
                'records': records, 'validRowCount': len(records) + len(unmatched) + len(ambiguous),
                'uniqueCurrentMeterCount': len(counts), 'members': sorted(counts),
                'duplicates': sorted(k for k, v in counts.items() if v > 1),
                'invalid': invalid, 'ambiguous': ambiguous, 'unmatched': unmatched}
    finally:
        book.close()


ALLOWED_PROJECTS = {"ireps2", "ireps-test", "ireps-5c3e9"}


def load_analytics_package(package, digest, project_id):
    from sales_june_baseline import exact_ids
    if (project_id not in ALLOWED_PROJECTS or package.get('projectId') != project_id
            or package.get('schemaVersion') != 2 or package.get('operation') != 'AMEND_ANALYTICS_JUNE_BASELINE'
            or package.get('month') != '2026-06' or package.get('lmPcode') != 'ZA5241'
            or package.get('provider') != 'contour'):
        raise ValueError('June analytics package scope/project mismatch')
    authority = json.loads(verified_bytes(package.get('sourceAuthority'), 'June source authority'))
    source = package['source']
    if (authority.get('authorityType') != 'USER_DESIGNATED_JUNE_CURRENT_METER_BASELINE'
            or authority.get('source') != source or authority.get('identityHeader') != 'MeterNumber'
            or authority.get('previousMeterNumberIsMembership') is not False
            or source.get('sheet') != 'Sheet1'):
        raise ValueError('June source authority mismatch')
    verified_bytes(package['savedOriginal'], 'Saved original June workbook')
    if package['savedOriginal']['sha256'] != source['sha256']:
        raise ValueError('Saved original and pinned June source differ')
    canonical = json.loads(verified_bytes(package['canonicalIdentityEvidence'], 'Canonical identity evidence'))
    if canonical.get('projectId') != project_id or canonical.get('collection') != 'sales-all-meters':
        raise ValueError('Canonical identity evidence project/collection mismatch')
    canonical_ids = canonical['members']
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError('Duplicate canonical evidence')
    membership = current_members(source, set(canonical_ids))
    if any(membership[k] for k in ('duplicates', 'invalid', 'ambiguous', 'unmatched')):
        raise ValueError('Unresolved June source identities')
    approved = membership['members']
    if not approved or package.get('expectedCount') != len(approved):
        raise ValueError('June observed/expected count mismatch')
    exact_ids(package.get('executionIds', []), approved)
    manifest = json.loads(verified_bytes(package['baselineIdManifest'], 'June ID manifest'))
    if manifest != membership:
        raise ValueError('June membership differs from current-meter source derivation')
    validate_identities(package)
    verified_bytes(package['attributionConfirmation'], 'June attribution confirmation')
    if not set(approved) <= creator_eligible_ids(package, project_id):
        raise ValueError('June identity outside confirmed creator scope')
    values, exceptions, aliases = ingest_workbook(source['path'], source['sha256'], source['sheet'], '2026-06', set(approved))
    exceptions = complete_exceptions(exceptions, values, approved)
    if package.get('categories') != values or package.get('exceptions') != exceptions:
        raise ValueError('June categories/exceptions differ from source')
    if json.loads(verified_bytes(package['identityReconciliation'], 'June comparisons')) != aliases:
        raise ValueError('June comparison mapping differs from source')
    old = [json.loads(line)['meterNoNormalized'] for line in verified_bytes(package['baseline'], 'Historical imported baseline').decode('utf-8-sig').splitlines() if line.strip()]
    reconciliation = json.loads(verified_bytes(package['membershipEvidence'], 'June reconciliation'))
    expected_diff = {'matched': sorted(set(approved) & set(old)), 'analyticsOnly': sorted(set(approved) - set(old)), 'oldBaselineOnly': sorted(set(old) - set(approved))}
    if reconciliation.get('oldBaselineComparison') != expected_diff or reconciliation.get('source') != source:
        raise ValueError('June historical comparison mismatch')
    snapshot = json.loads(verified_bytes(package['populationSnapshot'], 'June snapshot'))
    exact_ids(snapshot.get('members', []), approved)
    if (snapshot.get('schemaVersion') != 1 or snapshot.get('month') != '2026-06'
            or snapshot.get('lmPcode') != 'ZA5241' or snapshot.get('provider') != 'contour'
            or snapshot.get('sourceSha256') != source['sha256']
            or snapshot.get('previousSnapshotSha256') is not None or snapshot.get('replacements') != []
            or snapshot.get('completeness') != {'complete': True, 'evidenceSha256': package['membershipEvidence']['sha256']}):
        raise ValueError('June snapshot source/baseline contract mismatch')
    rows = [{'masterId': mid, 'expected': {'master': {'id': mid}, 'meterNoNormalized': mid,
             'provider': 'contour', 'lmPcode': 'ZA5241'}, 'categoryRefresh': {'month': '2026-06',
             'category': values.get(mid), 'creator': package['creator'], 'actor': package['actor'],
             'pipelineAttributionConfirmed': True, 'requiredHistory': {}}} for mid in approved]
    evidence = {'governedMonth': '2026-06', 'juneBaseline': package['baselineIdManifest'],
        'sourceAuthority': package['sourceAuthority'], 'historicalBaselineComparison': package['baseline'],
        'baselineIdManifest': package['baselineIdManifest'], 'exactJuneIdSetVerified': True,
        'categoryPackageSha256': digest, 'classificationSourceSha256': source['sha256'],
        'populationSnapshotSha256': package['populationSnapshot']['sha256'],
        'categoryExceptionDocumentIds': sorted(set(approved) - set(values)), 'categoryExceptions': exceptions,
        'allSourceExceptions': exceptions, 'monthlyCategoryPackage': {'month': '2026-06',
        'intendedRecords': len(approved), 'categoryRecords': len(values), 'source': source},
        'outsideJuneWritesPermitted': 0, 'documentCreatesPermitted': 0, 'snapshotFinalized': False}
    return rows, evidence, tuple(approved)
