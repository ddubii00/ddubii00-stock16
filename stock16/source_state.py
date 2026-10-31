"""Per-fund/component status prevents partial collectors from looking complete."""
from .model import now


def record_status(repo, entity_type, entity_id, component, collector, status,
                  message='', source_url=None, next_scheduled=None):
    checked=now()
    successful=checked if status in ('success','no_new_data') else None
    repo.execute('''INSERT INTO source_status(entity_type,entity_id,component,collector,status,
      message,last_check,last_success,next_scheduled,source_url) VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(entity_type,entity_id,component) DO UPDATE SET
      status=excluded.status,message=excluded.message,last_check=excluded.last_check,
      last_success=coalesce(excluded.last_success,source_status.last_success),
      next_scheduled=excluded.next_scheduled,source_url=excluded.source_url''',
      (entity_type,entity_id,component,collector,status,message,checked,successful,next_scheduled,source_url))


def queue_review(repo, collector, fund, asof, url, reason):
    repo.execute('''INSERT INTO history_review_queue VALUES(?,?,?,?,?,?)
      ON CONFLICT(collector,fund_id,as_of_date) DO UPDATE SET
      reason=excluded.reason,source_url=excluded.source_url''',
      (collector,fund,asof,url,reason,now()))
