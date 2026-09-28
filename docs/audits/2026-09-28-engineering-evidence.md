# Audit measured evidence — 2026-09-28
Commit: e5b326b2eaf691a638d030ad57acf1ce60016ef0
Method: tracked files only; physical lines include comments/blanks. Imports counted as unique target top-level packages per subtree, including local/TYPE_CHECKING imports; standard library excluded. Static cycles are not proof of runtime import failure.
Python source: 247 files, 52275 lines; non-test/non-migration/non-doc Python including scripts: 262 files, 55848 lines.
Tests: 190 Python files, 45174 lines; 185 test_*.py files.
Test functions: 2173; by subtree: {'architecture': 113, 'bench': 3, 'integration': 151, 'unit': 1906}
TODO markers: 0 []
asyncio.to_thread call sites: 95
## Module inventory
|Module|Python files|All files|Other nexus module targets|Third-party roots|
|---|---:|---:|---|---|
|__init__.py|1|1|||
|adapters|6|6|application,creative,domain,infrastructure,jobs|langgraph,numpy,structlog|
|agent|5|5|config,core,storage|httpx,psutil,sqlalchemy,sqlmodel,telegram|
|agents|11|11|config,llm,orchestration,personality,storage|sqlmodel|
|api|2|2|bot,config,core,creative,storage|fastapi,httpx,sqlalchemy,sqlmodel,starlette|
|application|9|9|config,creative||
|bot|26|26|adapters,agent,agents,api,application,config,creative,features,i18n,integrations,jobs,knowledge,observability,orchestration,presence,storage,worker|sqlalchemy,sqlmodel,structlog,telegram,uvicorn|
|cli.py|1|1|adapters,application,bot,config,continuum,creative,domain,infrastructure,llm,maintenance,memory,observability,orchestration,storage,tools,worker|typer|
|config|2|2||pydantic,pydantic_settings|
|continuum|6|6|||
|core|5|5|observability|aiosqlite,httpcore,httpx|
|creative|63|71|adapters,application,config,observability|PIL,aiosqlite,httpx,imageio_ffmpeg,nacl,numpy,pydantic|
|domain|6|6|||
|features|27|27|config,core,i18n,llm,observability,storage|PIL,arabic_reshaper,bidi,chromadb,flashrank,gtts,httpx,sqlalchemy,sqlmodel,structlog,telegram|
|i18n|2|17|observability||
|infrastructure|3|3|||
|integrations|3|3|core,observability|bs4,httpx|
|jobs|6|6|application,creative|PIL|
|knowledge|4|4|config,core,integrations,llm,observability,storage|sqlmodel|
|llm|8|8|config,features,observability|httpx,litellm,llama_cpp,sentence_transformers,structlog|
|maintenance|3|3|config,observability,storage,tools||
|memory|4|4|llm|sqlite_vec|
|observability|2|2||structlog|
|orchestration|4|4|agents,llm,memory,tools|langgraph|
|personality|2|2|||
|presence.py|1|1|||
|storage|28|31|adapters,application,config,domain,features,infrastructure,observability|alembic,boto3,botocore,httpx,langgraph,mega,psycopg,psycopg_pool,sqlalchemy,sqlmodel|
|tools|6|6|||
|worker.py|1|1|creative,features,jobs|pypdf|
## Model definitions (all SQLModel tables, exact annotations)
### Referral [FILE:src/nexus_ai_agent/features/referral.py:22-35]
id: int | None = Field(default=None, primary_key=True); referrer_id: int = Field(index=True); referee_id: int = Field(index=True, unique=True); referral_code: str = Field(index=True); status: str = Field(default='pending', index=True); reward_claimed: bool = Field(default=False); xp_awarded: bool = Field(default=False); created_at: datetime = Field(default_factory=datetime.utcnow, index=True); completed_at: datetime | None = Field(default=None)
### ReferralCode [FILE:src/nexus_ai_agent/features/referral.py:38-48]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(index=True, unique=True); code: str = Field(index=True, unique=True); total_referrals: int = Field(default=0); successful_referrals: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow)
### User [FILE:src/nexus_ai_agent/storage/models.py:8-13]
id: int | None = Field(default=None, primary_key=True); telegram_id: int = Field(index=True, unique=True); username: str = Field(default=''); created_at: datetime = Field(default_factory=datetime.utcnow, index=True); is_allowed: bool = Field(default=True, index=True)
### Chat [FILE:src/nexus_ai_agent/storage/models.py:16-21]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True, unique=True); thread_id: str = Field(index=True); created_at: datetime = Field(default_factory=datetime.utcnow, index=True); policy: str = Field(default='default')
### Message [FILE:src/nexus_ai_agent/storage/models.py:24-30]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(foreign_key='chat.id', index=True); role: str = Field(index=True); content: str; correlation_id: str = Field(index=True); created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
### Task [FILE:src/nexus_ai_agent/storage/models.py:33-39]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(foreign_key='chat.id', index=True); status: str = Field(default='pending', index=True); plan_json: str = Field(default='{}'); created_at: datetime = Field(default_factory=datetime.utcnow, index=True); completed_at: datetime | None = Field(default=None, index=True)
### ToolRun [FILE:src/nexus_ai_agent/storage/models.py:42-50]
id: int | None = Field(default=None, primary_key=True); task_id: int | None = Field(default=None, foreign_key='task.id', index=True); tool_name: str = Field(index=True); input_json: str = Field(default='{}'); output_json: str = Field(default='{}'); error: str | None = Field(default=None); duration_ms: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
### WelcomeMessage [FILE:src/nexus_ai_agent/storage/models.py:56-62]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True, unique=True); text: str = Field(default=''); created_at: datetime = Field(default_factory=datetime.utcnow)
### ChannelSchedule [FILE:src/nexus_ai_agent/storage/models.py:65-73]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True); text: str; scheduled_at: datetime = Field(index=True); status: str = Field(default='pending', index=True); created_at: datetime = Field(default_factory=datetime.utcnow)
### AnonSession [FILE:src/nexus_ai_agent/storage/models.py:76-83]
id: int | None = Field(default=None, primary_key=True); user1_id: int = Field(index=True); user2_id: int = Field(index=True); started_at: datetime = Field(default_factory=datetime.utcnow, index=True); status: str = Field(default='active', index=True)
### QuizScore [FILE:src/nexus_ai_agent/storage/models.py:86-95]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(index=True); chat_id: int = Field(index=True); score: int = Field(default=0); answered: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow); updated_at: datetime = Field(default_factory=datetime.utcnow)
### Reminder [FILE:src/nexus_ai_agent/storage/models.py:98-107]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(index=True); chat_id: int = Field(index=True); text: str; remind_at: datetime = Field(index=True); status: str = Field(default='pending', index=True); created_at: datetime = Field(default_factory=datetime.utcnow)
### AdminLog [FILE:src/nexus_ai_agent/storage/models.py:113-121]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(index=True); action: str = Field(index=True); target: str = Field(default=''); details: str = Field(default=''); timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
### ForceJoinConfig [FILE:src/nexus_ai_agent/storage/models.py:124-132]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True, unique=True); enabled: bool = Field(default=False); channel_username: str = Field(default='@nexus_ai_official'); welcome_message: str = Field(default='⛔ لطفاً ابتدا در کانال عضو شوید.'); created_at: datetime = Field(default_factory=datetime.utcnow)
### PersonalityConfig [FILE:src/nexus_ai_agent/storage/models.py:135-142]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True, unique=True); personality: str = Field(default='friendly'); set_by: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow)
### EngagementConfig [FILE:src/nexus_ai_agent/storage/models.py:145-153]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True, unique=True); enabled: bool = Field(default=False); frequency_minutes: int = Field(default=60); last_engagement: datetime | None = Field(default=None); created_at: datetime = Field(default_factory=datetime.utcnow)
### ModerationConfig [FILE:src/nexus_ai_agent/storage/models.py:156-167]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True, unique=True); anti_spam: bool = Field(default=True); anti_flood: bool = Field(default=True); link_filter: bool = Field(default=False); profanity_filter: bool = Field(default=False); max_warnings: int = Field(default=3); mute_duration_minutes: int = Field(default=30); created_at: datetime = Field(default_factory=datetime.utcnow)
### UserReputation [FILE:src/nexus_ai_agent/storage/models.py:170-181]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(index=True); chat_id: int = Field(index=True); reputation: int = Field(default=0); warnings: int = Field(default=0); is_muted: bool = Field(default=False); mute_until: datetime | None = Field(default=None); created_at: datetime = Field(default_factory=datetime.utcnow); updated_at: datetime = Field(default_factory=datetime.utcnow)
### UserXP [FILE:src/nexus_ai_agent/storage/models.py:184-199]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(index=True); chat_id: int = Field(index=True); xp: int = Field(default=0); level: int = Field(default=1); streak: int = Field(default=0); last_daily: datetime | None = Field(default=None); achievements: str = Field(default='[]'); referral_count: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow); updated_at: datetime = Field(default_factory=datetime.utcnow)
### AdCampaign [FILE:src/nexus_ai_agent/storage/models.py:202-214]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True); text: str; interval_hours: float = Field(default=6.0); status: str = Field(default='active', index=True); repeat_count: int = Field(default=0); max_repeats: int = Field(default=0); next_run: datetime | None = Field(default=None); created_by: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow)
### ViralPost [FILE:src/nexus_ai_agent/storage/models.py:217-226]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True); text: str; viral_score: float = Field(default=0.0); status: str = Field(default='pending', index=True); posted_at: datetime | None = Field(default=None); created_at: datetime = Field(default_factory=datetime.utcnow)
### AnalyticsEvent [FILE:src/nexus_ai_agent/storage/models.py:229-237]
id: int | None = Field(default=None, primary_key=True); chat_id: int = Field(index=True); user_id: int = Field(default=0); event_type: str = Field(index=True); event_data: str = Field(default='{}'); created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
### Referral [FILE:src/nexus_ai_agent/storage/models.py:243-255]
id: int | None = Field(default=None, primary_key=True); referrer_id: int = Field(default=0); referee_id: int = Field(default=0); referral_code: str = Field(default=''); status: str = Field(default='pending'); reward_claimed: bool = Field(default=False); xp_awarded: bool = Field(default=False); created_at: datetime = Field(default_factory=datetime.utcnow); completed_at: datetime | None = Field(default=None)
### ReferralCode [FILE:src/nexus_ai_agent/storage/models.py:258-267]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(default=0); code: str = Field(default=''); total_referrals: int = Field(default=0); successful_referrals: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow)
### UserLanguage [FILE:src/nexus_ai_agent/storage/models.py:270-278]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(default=0); language: str = Field(default='en'); created_at: datetime = Field(default_factory=datetime.utcnow); updated_at: datetime = Field(default_factory=datetime.utcnow)
### CloudFile [FILE:src/nexus_ai_agent/storage/models.py:281-291]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(default=0); file_name: str = Field(default=''); provider: str = Field(default=''); remote_path: str = Field(default=''); file_size: int = Field(default=0); created_at: datetime = Field(default_factory=datetime.utcnow)
### KnowledgeCache [FILE:src/nexus_ai_agent/storage/models.py:297-303]
id: int | None = Field(default=None, primary_key=True); query: str = Field(index=True); source: str; content: str; expires_at: datetime = Field(index=True); created_at: datetime = Field(default_factory=datetime.utcnow)
### PendingApproval [FILE:src/nexus_ai_agent/storage/models.py:306-312]
id: int | None = Field(default=None, primary_key=True); change_type: str; description: str; created_at: datetime = Field(default_factory=datetime.utcnow); status: str = 'pending'; auto_apply_at: datetime | None = None
### UserActiveAgent [FILE:src/nexus_ai_agent/storage/models.py:315-318]
user_id: int = Field(primary_key=True); agent_name: str; activated_at: datetime = Field(default_factory=datetime.utcnow)
### UserMemory [FILE:src/nexus_ai_agent/storage/models.py:321-336]
user_id: int = Field(primary_key=True); name: str | None = None; interests: str = '[]'; occupation: str | None = None; personality_tags: str = '[]'; last_updated: datetime = Field(default_factory=datetime.utcnow); ai_memory_consent: str | None = None; ai_memory_consent_at: datetime | None = None; ai_memory_prompted: bool | None = None
### DocumentChunk [FILE:src/nexus_ai_agent/storage/models.py:339-345]
id: int | None = Field(default=None, primary_key=True); user_id: int = Field(index=True); filename: str; chunk_text: str; chunk_index: int; embedding: str
## Raw SQL execution sites (storage subtree)
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:208-211] `connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:235-237] `connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'")`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:240-242] `connection.execute('SELECT version_num FROM alembic_version ORDER BY version_num')`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:83-85] `self._connection.execute('SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id')`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:91-95] `self._connection.execute('SELECT thread_id, checkpoint_id, parent_checkpoint_id FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:102-105] `self._connection.execute('SELECT 1 FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ? LIMIT 1', (thread_id, checkpoint_id))`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:149-153] `self._connection.execute('SELECT channel, version FROM checkpoint_blobs WHERE thread_id = ? ORDER BY channel, version', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:215-215] `connection.execute(f'PRAGMA table_info("{name}")')`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:217-217] `connection.execute(f'PRAGMA foreign_key_list("{name}")')`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:219-219] `connection.execute(f'PRAGMA index_list("{name}")')`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:119-124] `self._connection.execute('SELECT COALESCE(SUM(LENGTH(checkpoint)), 0) + COALESCE(SUM(LENGTH(metadata)), 0) FROM checkpoints WHERE thread_id = ?', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:127-130] `self._connection.execute('SELECT COALESCE(SUM(LENGTH(value)), 0) FROM writes WHERE thread_id = ?', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:133-136] `self._connection.execute('SELECT COALESCE(SUM(LENGTH(blob)), 0) FROM checkpoint_blobs WHERE thread_id = ?', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:139-142] `self._connection.execute('SELECT COALESCE(SUM(LENGTH(value)), 0) FROM checkpoint_writes WHERE thread_id = ?', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_adapter.py:200-202] `self._connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_fingerprint.py:45-45] `connection.execute('SELECT version_num FROM alembic_version')`
[FILE:src/nexus_ai_agent/storage/checkpoint_fingerprint.py:39-41] `connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'")`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_pg_store.py:89-89] `self._connect().execute(sql, params)`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_pg_store.py:97-97] `self._connect().execute(sql, params)`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_pg_store.py:93-93] `self._connect().execute(sql, params)`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_store.py:32-41] `self._connection.execute(f'CREATE TABLE IF NOT EXISTS {_TABLE} (\n                thread_id TEXT NOT NULL,\n                checkpoint_id TEXT NOT NULL,\n                created_at TEXT NOT NULL,\n                last_accessed_at TEXT,\n                active_until TEXT,\n                PRIMARY KEY (thread_id, checkpoint_id)\n            )')`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_store.py:49-57] `self._connection.execute(f'INSERT INTO {_TABLE}\n            (thread_id, checkpoint_id, created_at, last_accessed_at, active_until)\n            VALUES (?, ?, ?, ?, ?)\n            ON CONFLICT(thread_id, checkpoint_id) DO UPDATE SET\n              last_accessed_at=excluded.last_accessed_at,\n              active_until=excluded.active_until', (record.thread_id, record.checkpoint_id, *values))`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_store.py:83-86] `self._connection.execute(f'UPDATE {_TABLE} SET last_accessed_at = ? WHERE thread_id = ?', (_key(accessed_at), thread_id))`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_store.py:92-95] `self._connection.execute(f'DELETE FROM {_TABLE} WHERE thread_id = ? AND checkpoint_id = ?', (record.thread_id, record.checkpoint_id))`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_store.py:61-64] `self._connection.execute(f'SELECT thread_id, checkpoint_id, created_at, last_accessed_at, active_until FROM {_TABLE}')`
[FILE:src/nexus_ai_agent/storage/checkpoint_lifecycle_store.py:100-103] `self._connection.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' ORDER BY name")`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:269-269] `connection.execute("SELECT to_regclass('public.' || %s) IS NOT NULL", (name,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:276-278] `connection.execute('SELECT version_num FROM alembic_version ORDER BY version_num')`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:102-104] `self._connection.execute("SELECT to_regclass('public.' || %s) IS NOT NULL", (name,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:110-112] `self._connection.execute('SELECT thread_id FROM checkpoints GROUP BY thread_id ORDER BY thread_id')`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:118-122] `self._connection.execute('SELECT thread_id, checkpoint_id, parent_checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:129-132] `self._connection.execute('SELECT 1 FROM checkpoints WHERE thread_id = %s AND checkpoint_id = %s LIMIT 1', (thread_id, checkpoint_id))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:174-178] `self._connection.execute('SELECT channel, version FROM checkpoint_blobs WHERE thread_id = %s ORDER BY channel, version', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:249-249] `connection.execute(_PK_QUERY, (name,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:250-250] `connection.execute(_INDEXES_QUERY, (name,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:145-150] `self._connection.execute('SELECT COALESCE(SUM(pg_column_size(checkpoint) + pg_column_size(metadata)), 0) FROM checkpoints WHERE thread_id = %s', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:154-158] `self._connection.execute('SELECT COALESCE(SUM(pg_column_size(blob)), 0) FROM checkpoint_writes WHERE thread_id = %s', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:162-166] `self._connection.execute('SELECT COALESCE(SUM(pg_column_size(blob)), 0) FROM checkpoint_blobs WHERE thread_id = %s', (thread_id,))`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:234-236] `self._connection.execute('SELECT version_num FROM alembic_version ORDER BY version_num')`
[FILE:src/nexus_ai_agent/storage/checkpoint_pg_adapter.py:248-248] `connection.execute(_COLUMNS_QUERY, (name,))`
[FILE:src/nexus_ai_agent/storage/db.py:274-274] `conn.execute(text('PRAGMA journal_mode=WAL'))`
[FILE:src/nexus_ai_agent/storage/langgraph_checkpoint.py:293-293] `conn.execute('PRAGMA journal_mode=WAL')`

## GitHub and Git measurement
Observation: 2026-09-28; PR ages frozen at 09:00 UTC. Commands: `gh pr list --limit 200 --json number,title,author,createdAt,body,headRefName`; `gh pr list --state merged --search merged:>=2026-08-29 --limit 1000 --json number,mergedAt,author`; `gh api --paginate repos/bot523h/nexus-ai-agent/contributors`.
Open PRs: 33; all 33 authored by app/arena-ai-coding-agent (AI-app attribution, not an independent provenance audit of every patch). Age <1 day: 7; 1–<7 days: 26; >=7 days: 0.
Merged since 2026-08-29: 69 (API query to observation time, not a closed future day).
GitHub contributor accounts: 5; 1 Bot-typed, 4 User-typed. User-typed is NOT proof of human authorship.
- bot523h: User, 271 API-attributed contributions.
- arena-ai-coding-agent[bot]: Bot, 60 API-attributed contributions.
- nexus-ai-agent: User, 19 API-attributed contributions.
- solo-bot: User, 8 API-attributed contributions.
- nexus: User, 8 API-attributed contributions.
|PR|Created UTC|AI-app authored|
|---|---|---|
|115|2026-09-28T06:32:06Z|yes|
|114|2026-09-27T21:05:27Z|yes|
|113|2026-09-27T21:03:28Z|yes|
|112|2026-09-27T19:09:29Z|yes|
|111|2026-09-27T18:43:49Z|yes|
|105|2026-09-27T11:34:33Z|yes|
|104|2026-09-27T11:16:02Z|yes|
|100|2026-09-27T07:56:44Z|yes|
|99|2026-09-26T21:38:54Z|yes|
|97|2026-09-26T21:18:30Z|yes|
|96|2026-09-26T21:00:36Z|yes|
|94|2026-09-26T18:49:47Z|yes|
|93|2026-09-26T05:22:16Z|yes|
|92|2026-09-26T05:15:26Z|yes|
|89|2026-09-25T22:18:08Z|yes|
|88|2026-09-25T21:23:11Z|yes|
|87|2026-09-25T20:29:37Z|yes|
|84|2026-09-25T19:52:45Z|yes|
|83|2026-09-25T17:45:45Z|yes|
|73|2026-09-24T17:44:48Z|yes|
|70|2026-09-24T16:48:31Z|yes|
|69|2026-09-24T16:34:56Z|yes|
|68|2026-09-24T13:45:10Z|yes|
|67|2026-09-24T11:14:22Z|yes|
|66|2026-09-24T10:25:48Z|yes|
|64|2026-09-24T09:31:12Z|yes|
|63|2026-09-24T05:38:34Z|yes|
|60|2026-09-23T19:54:12Z|yes|
|59|2026-09-23T17:41:57Z|yes|
|58|2026-09-23T17:37:19Z|yes|
|57|2026-09-23T17:23:04Z|yes|
|56|2026-09-22T23:15:38Z|yes|
|33|2026-09-21T11:55:35Z|yes|
Full history fetched with `git fetch --unshallow origin` (no branch switch). Weekly metric: committer timestamps on HEAD ancestry, all parents, half-open UTC Monday bins; includes merges; does not count commits on other branches.
|Week beginning|Commits|
|---|---:|
|2026-07-06|0|
|2026-07-13|0|
|2026-07-20|0|
|2026-07-27|0|
|2026-08-03|0|
|2026-08-10|0|
|2026-08-17|0|
|2026-08-24|0|
|2026-08-31|1|
|2026-09-07|0|
|2026-09-14|123|
|2026-09-21|220|
Latest version-sorted fetched tag: v3.5.0. Tag refs fetched from origin; current branch unchanged.
## Execution evidence
Environment: CPython 3.11.2, isolated venv. Full `pip install -e ".[dev]"` stopped at 180 seconds building llama-cpp-python; reduced install excludes sentence-transformers, llama-cpp-python, chromadb, flashrank, langchain-community, litellm. No live Telegram/LLM/cloud request was made.
### targeted
Selected files: tests.integration.test_graph, tests.integration.test_in_process_job_queue, tests.unit.test_access_guard, tests.unit.test_ai_memory_consent, tests.unit.test_api_ssrf_download, tests.unit.test_auth_middleware, tests.unit.test_dashboard_api, tests.unit.test_http_client_ssrf, tests.unit.test_image_gen_adapters, tests.unit.test_phase0_command_fixes, tests.unit.test_safe_calculator.TestArithmetic, tests.unit.test_safe_calculator.TestCalculatorWrapper, tests.unit.test_safe_calculator.TestDoSBoundaries, tests.unit.test_safe_calculator.TestFunctions, tests.unit.test_safe_calculator.TestMathErrors, tests.unit.test_safe_calculator.TestPersianInput, tests.unit.test_safe_calculator.TestSecurity, tests.unit.test_safe_paths.TestSafeJoin, tests.unit.test_safe_paths.TestSanitizeFileName, tests.unit.test_security_hardening, tests.unit.test_slideshow_render, tests.unit.test_surface_ads, tests.unit.test_surface_channel_management, tests.unit.test_surface_docs, tests.unit.test_surface_gamification
Result: 372 passed, 13 warnings in 49.13s
### targeted2
Selected files: tests.unit.test_db, tests.unit.test_feature_wiring, tests.unit.test_gamification_daily, tests.unit.test_graph_memory, tests.unit.test_i18n_parity, tests.unit.test_litellm_provider, tests.unit.test_local_server_provider, tests.unit.test_memory_recall, tests.unit.test_observability, tests.unit.test_pack_manifest_verify, tests.unit.test_pack_runtime_composition, tests.unit.test_structured_events
Result: 1 failed, 198 passed, 1 skipped, 3 warnings in 18.10s
The single failure is test_factory_builds_routing_chain_wrapped_in_fallback: LiteLLM deliberately absent; not counted as a reproduced application defect. One optional test skipped. Combined passes: 570 (not a full-suite coverage run).
### Actual-session reproduction
Command: call `_upsert_user(lambda: get_session(temp_sqlite), SimpleNamespace(id=123, username="audit"))` with a fresh real DB; no patched session.
```text
Session: sqlalchemy.ext.asyncio.session.AsyncSession has exec: False
upsert reproduction: AttributeError 'AsyncSession' object has no attribute 'exec'
```
### Wheel inspection
Command: `pip wheel . --no-deps -w /tmp/nexus-audit/wheels`; inspect ZIP entries. Result: 253 entries, 0 JSON files, 0 locale files, 0 pack manifests, 0 migration files. This is the regular wheel path, not editable-install behavior.
### CI at audited commit
GitHub run https://github.com/bot523h/nexus-ai-agent/actions/runs/36339890292 : success; 16/16 jobs success. Branch-protection API: HTTP 403, Resource not accessible by integration (authorization visibility, not proof protections absent). Artifact ZIP download failed twice with EOF; coverage read from check-run annotations instead.
continuum coverage py3.12 @ e5b326b2eaf691a638d030ad57acf1ce60016ef0: accepted=True commit=e5b326b2eaf691a638d030ad57acf1ce60016ef0 total=97.38 audio=96.56 caption=97.12 core=95.95 delivery=100.0 edit=96.46 motion=98.55 slideshow=97.81
continuum coverage py3.11 @ e5b326b2eaf691a638d030ad57acf1ce60016ef0: accepted=True commit=e5b326b2eaf691a638d030ad57acf1ce60016ef0 total=97.37 audio=96.55 caption=97.09 core=95.89 delivery=100.0 edit=96.46 motion=98.55 slideshow=97.89
continuum coverage py3.10 @ e5b326b2eaf691a638d030ad57acf1ce60016ef0: accepted=True commit=e5b326b2eaf691a638d030ad57acf1ce60016ef0 total=97.37 audio=96.55 caption=97.09 core=95.89 delivery=100.0 edit=96.46 motion=98.55 slideshow=97.89
Global coverage: unavailable. No tracked .coverage/coverage.xml; CI annotations measure selected creative packs/core, not all src.
## Dependency/security measurements
Reduced installed-environment pip-audit returned 20 records on 2 tooling packages (pip 23.0.1, setuptools 66.1.1), with duplicate aliases. No findings on other installed packages; six heavy dependencies omitted, so NOT a clean production bill of health.
pip 23.0.1: examples include CVE-2023-5752 and CVE-2025-8869; tooling environment only.
setuptools 66.1.1: examples include CVE-2024-6345 and CVE-2025-47273; tooling environment only.
Bulk lower-bound audit failed with PyPI HTTP 503 for pytest-mock/3.12. Five direct version-metadata queries succeeded; these show vulnerable ALLOWED minima, not deployed versions. Exact source URLs and CVEs:
- python-multipart 0.0.9 — https://pypi.org/pypi/python-multipart/0.0.9/json: CVE-2024-53981 fixed 0.0.18; CVE-2026-24486 fixed 0.0.22. Examples, not an exhaustive advisory list.
- pillow 10.0.0 — https://pypi.org/pypi/pillow/10.0.0/json: CVE-2023-50447 fixed 10.2.0; CVE-2024-28219 fixed 10.3.0.
- langchain-community 0.2.0 — https://pypi.org/pypi/langchain-community/0.2.0/json: CVE-2024-5998 fixed 0.2.4; CVE-2024-8309 fixed 0.2.19. Vulnerable modules need not be invoked by this application.
- setuptools 69.0.0 — https://pypi.org/pypi/setuptools/69.0.0/json: CVE-2024-6345 fixed 70.0.0; CVE-2025-47273 fixed 78.1.1.
- litellm 1.74.0 — https://pypi.org/pypi/litellm/1.74.0/json returns advisories; no exploitability conclusion drawn. SDK use must not be equated with exposing the separately vulnerable proxy/admin service.
History scan: `git log HEAD --all -p --no-ext-diff`, high-specificity Telegram/Google/GitHub token shapes, added lines only. 1 match: tests/unit/test_observability.py test fixture; 0 confirmed live credentials. No .env path history found. Not an entropy scan, not proof of no secrets; fixture validity not probed.
Source AST searches: 0 JWT-library imports/signing implementations, 0 Prometheus/StatsD/OpenTelemetry imports, 0 explicit ThreadPoolExecutor/set_default_executor calls, 0 literal truth-constant assertions; 95 to_thread call sites.
## Locale parity
- ar.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- de.json: 79 keys, 0 missing vs English, 4 exact English-value matches (not automatically untranslated).
- en.json: 79 keys, 0 missing vs English, 79 exact English-value matches (not automatically untranslated).
- es.json: 79 keys, 0 missing vs English, 3 exact English-value matches (not automatically untranslated).
- fa.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- fr.json: 79 keys, 0 missing vs English, 3 exact English-value matches (not automatically untranslated).
- hi.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- id.json: 79 keys, 0 missing vs English, 3 exact English-value matches (not automatically untranslated).
- it.json: 79 keys, 0 missing vs English, 4 exact English-value matches (not automatically untranslated).
- ja.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- ko.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- pt.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- ru.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- tr.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
- zh.json: 79 keys, 0 missing vs English, 2 exact English-value matches (not automatically untranslated).
## Declared dependency inventory
Runtime: 32 entries; exact pins 10, lower-only 21, bounded ranges 1.
build: `setuptools>=69.0`, `wheel`
runtime: `python-telegram-bot>=21.0`, `langgraph>=0.2`, `langgraph-checkpoint-sqlite>=1.0`, `langchain-community>=0.2`, `sqlmodel==0.0.42`, `sqlalchemy==2.0.54`, `alembic==1.20.0`, `aiosqlite==0.22.1`, `asyncpg==0.31.0`, `psycopg[binary,pool]==3.3.5`, `langgraph-checkpoint-postgres==3.1.2`, `pydantic-settings>=2.0`, `sentence-transformers>=3.0`, `llama-cpp-python>=0.2`, `structlog>=24.0`, `sqlite-vec>=0.1`, `typer>=0.12`, `httpx==0.28.1`, `python-multipart>=0.0.9`, `duckduckgo-search>=8.0`, `fastapi>=0.110`, `uvicorn==0.53.0`, `numpy>=1.26`, `chromadb>=0.5`, `flashrank>=0.2`, `arabic-reshaper>=3.0`, `python-bidi>=0.4`, `pillow>=10.0`, `beautifulsoup4>=4.12`, `psutil>=5.9`, `litellm>=1.74,<2`, `boto3==1.43.98`
pdf: `pypdf>=5.1`
speech: `faster-whisper>=1.0,<2`
translate: `argostranslate>=1.9,<2`
dev: `pytest>=8.0`, `pytest-asyncio>=0.23`, `ruff>=0.4`, `mypy>=1.10`, `pytest-mock>=3.12`, `pypdf>=5.1`, `imageio-ffmpeg>=0.5`
## Scope and limits
All 247 source Python files and all test Python files were AST-parsed for inventories/imports/comments/signatures; this is NOT a claim every function body was manually reviewed. All storage raw execute sites are transcribed above; named SQL helpers in checkpoint_lifecycle_pg_store.py:48-60,87-98,112-155 and checkpoint_pg_adapter.py:50-73 were also inspected.
Large-file excerpt disclosure (not a full-body reading claim): bot/handlers.py:127-255,280-445,448-834,837-1004,1134-1210,1300-1582; other regions indexed via AST signatures/calls. adapters/in_process_job_queue.py:277-305,358-435,748-819 plus targeted lifecycle fragments; not all CAS/verification branches manually read. creative/slideshow/ffmpeg.py:370-450,480-567,629-711 plus signatures/constants and selected rendering fragments. creative/packs/*/operations.py: concrete bodies and registration/signature inventories sampled, not every operation independently rendered. continuum/* evidence/mutation catalogs inventoried; CI annotations inspected, not every mutation body manually read. Initial migration table declarations AST-inventoried and chain/downgrades inspected; full SQLModel fields above. Most large tests were sampled rather than manually read or run in full; selected executed files are listed above.
External services, live quotas, actual deployed environment, human authorship, full secret history assurance, and merge enforcement remain unverified.
