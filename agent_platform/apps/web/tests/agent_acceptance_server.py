"""Disposable persisted API fixtures, no request mocking or production database."""
import os
import argparse
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, select, update
import uvicorn
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure import tables as t, tenancy_tables as nt
from agent_platform.apps.api.main import create_app
from agent_platform.modules.platform import hasher, now

parser = argparse.ArgumentParser(description="Start the real API with fresh disposable Agent acceptance fixtures.")
parser.add_argument("--directory", required=True, type=Path, help="A new directory; existing directories are rejected.")
parser.add_argument("--port", type=int, default=8033)
args = parser.parse_args()
run = args.directory.expanduser().resolve()
run.mkdir(parents=True, exist_ok=False)
print(f"Disposable Agent acceptance database: {run / 'acceptance.db'}", flush=True)
os.environ['PLATFORM_DATABASE_URL'] = f'sqlite:///{run / "acceptance.db"}'
command.upgrade(Config('agent_platform/alembic.ini'), 'head')
settings = Settings(database_url=os.environ['PLATFORM_DATABASE_URL'], admin_email='admin@example.test', admin_password='Agent-ui-acceptance-2026', default_model='gpt-4o', default_model_input_price='1', default_model_output_price='3', web_directory=run / 'unused-web', tombstone_path=run / 'tombstones.jsonl', export_directory=run / 'exports')
app = create_app(settings)
p, db = app.state.platform, app.state.database
p.bootstrap()
with db.read() as c:
    owner = dict(c.execute(select(t.users).where(t.users.c.email == settings.admin_email)).mappings().one())
    model = c.scalar(select(t.models.c.id).where(t.models.c.alias == 'gpt-4o'))
tenant = owner['tenant_id']
if not (run / 'fixtures-ready').exists():
    with db.transaction('acceptance-fixtures') as c:
        c.execute(delete(t.agent_versions))
        c.execute(delete(t.agents))
        c.execute(update(t.models).where(t.models.c.id == model).values(max_output_tokens=8192))
        c.execute(update(t.tenants).where(t.tenants.c.id == tenant).values(name='工作空间'))
        for aid, name, desc, mid, prompt, version in [
            ('agent_calculator','计算助手','数学运算与时间查询',model,'你是严谨的计算助手。先理解问题，再使用可用工具完成计算或查询当前时间，最后清晰解释结果。',2),
            ('agent_content_editor','内容助手','根据提示词整理内容',None,'根据提示词整理内容。',1),
            ('agent_draft_helper','新建助手','',None,'你是一位严谨、友好的智能助手。',0),
        ]:
            spec = dict(id=aid,tenant_id=tenant,name=name,description=desc,system_prompt=prompt,model_id=mid,temperature=0.7,max_steps=8,max_tokens=4096,tools=['calculator','current_time'],published_version=version,created_at={'agent_calculator':'2026-10-03T03:00:00Z','agent_content_editor':'2026-10-02T03:00:00Z','agent_draft_helper':'2026-10-01T03:00:00Z'}[aid])
            c.execute(insert(t.agents).values(**spec))
            for v in range(1,version+1): c.execute(insert(t.agent_versions).values(agent_id=aid,tenant_id=tenant,version=v,spec=spec,created_at=now()))
        for uid, email, role in [('acceptance_member','member@example.test','member'),('acceptance_finance','finance@example.test','finance_viewer')]:
            c.execute(insert(t.users).values(id=uid,tenant_id=tenant,email=email,name=role,role='member',active=True,password_hash=hasher.hash(settings.admin_password),created_at=now()))
            c.execute(insert(nt.memberships).values(id=uid+'-membership',tenant_id=tenant,user_id=uid,role=role,status='active',joined_at=now()))
    (run / 'fixtures-ready').write_text(tenant)
uvicorn.run(app,host='127.0.0.1',port=args.port)
