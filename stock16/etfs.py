"""Explicit issuer-reviewed classification; no inference from fund performance."""
SCOPES=('active','all','passive')


def catalog(repo,scope='all'):
    if scope not in SCOPES: raise ValueError('Invalid ETF scope')
    rows=repo.rows('''SELECT f.*,w.enabled,coalesce(p.style,'unknown') style,
      coalesce(p.listing_country,'KR') listing_country,p.manager,p.strategy,p.evidence_url,p.verified_on
      FROM funds f JOIN etf_watchlist w ON w.fund_id=f.id LEFT JOIN etf_profiles p ON p.fund_id=f.id
      ORDER BY p.listing_country,p.manager,f.name''')
    return rows if scope=='all' else [r for r in rows if r['style']==scope]


def in_focus(repo,scope='active'):
    return {r['id'] for r in catalog(repo,scope) if r['enabled']}
