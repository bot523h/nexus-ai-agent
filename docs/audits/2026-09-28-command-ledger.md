# README command-status ledger
Scope: ONLY README.md:310-311; expand wildcard names against literal main registrations, count each named command once. Force-join means explicitly named forcejoin_on, not every related command; prose parentheticals do not add commands. These 86 entries are not the entire product.
I/T = implemented with selected component/surface tests executed; I/U = implemented code path, behavioral success not established by the selected tests for this command; P = known partial/broken contract; S = canned/stub; M = documented command not found among registrations. External integrations remain untested live even when I/T. To avoid overclaiming, original Real entries are I/U unless this ledger supplies a selected surface-test proof; other test files may exist.
Literal CommandHandler registrations in handlers.py: 112; expanded Real: 54; expanded Simulated: 32.
|README|Command|Audit|Evidence / defect|
|---|---|---|---|
|Real|/ai|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1408] [FILE:src/nexus_ai_agent/bot/handlers.py:342-353]|
|Real|/ask|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1409] [FILE:src/nexus_ai_agent/bot/handlers.py:355-356]|
|Real|/code|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1410] [FILE:src/nexus_ai_agent/bot/handlers.py:358-365]|
|Real|/translate|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1411] [FILE:src/nexus_ai_agent/bot/handlers.py:367-374]|
|Real|/summarize|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1413] [FILE:src/nexus_ai_agent/bot/handlers.py:536-564]|
|Real|/image|P|path versus file_path; [FILE:src/nexus_ai_agent/bot/handlers.py:1415] [FILE:src/nexus_ai_agent/bot/handlers.py:388-421]|
|Real|/imagine|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1416] [FILE:src/nexus_ai_agent/bot/handlers.py:423-445]|
|Real|/slideshow|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1470] [FILE:src/nexus_ai_agent/bot/slideshow_handlers.py:72-107]|
|Real|/tts|P|path versus file_path; gTTS undeclared; [FILE:src/nexus_ai_agent/bot/handlers.py:1418] [FILE:src/nexus_ai_agent/bot/handlers.py:448-479]|
|Real|/stt|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1419] [FILE:src/nexus_ai_agent/bot/handlers.py:482-533]|
|Real|/cloud|P|remote_key versus remote_path; missing tenant key; [FILE:src/nexus_ai_agent/bot/handlers.py:1421] [FILE:src/nexus_ai_agent/bot/handlers.py:567-630]|
|Real|/myfiles|P|AsyncSession.exec absent; [FILE:src/nexus_ai_agent/bot/handlers.py:1422] [FILE:src/nexus_ai_agent/bot/handlers.py:633-652]|
|Real|/download|P|AsyncSession.exec absent; [FILE:src/nexus_ai_agent/bot/handlers.py:1423] [FILE:src/nexus_ai_agent/bot/handlers.py:655-706]|
|Real|/cloud_status|P|static advertised quotas, not measured usable capacity; [FILE:src/nexus_ai_agent/bot/handlers.py:1424] [FILE:src/nexus_ai_agent/bot/handlers.py:709-711]|
|Real|/referral|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1426] [FILE:src/nexus_ai_agent/bot/handlers.py:714-724]|
|Real|/referral_board|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1427] [FILE:src/nexus_ai_agent/bot/handlers.py:727-730]|
|Real|/start|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1301]|
|Real|/calc|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1341]|
|Real|/remind|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1336]|
|Real|/cancel_remind|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1337]|
|Real|/reminds|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1338]|
|Real|/tr|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1339]|
|Real|/convert|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1340]|
|Real|/quiz|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1325]|
|Real|/guess_start|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1327]|
|Real|/guess|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1329]|
|Real|/guess_stop|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1328]|
|Real|/wordle|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1330]|
|Real|/wordle_stop|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1331]|
|Real|/poll|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1332]|
|Real|/anon_start|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1318]|
|Real|/anon_stop|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1319]|
|Real|/anon_report|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1320]|
|Real|/forcejoin_on|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1364] [FILE:src/nexus_ai_agent/bot/handlers.py:1013-1026]|
|Real|/owner|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1358] [FILE:src/nexus_ai_agent/bot/handlers.py:925-931]|
|Real|/system|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1359] [FILE:src/nexus_ai_agent/bot/handlers.py:933-938]|
|Real|/broadcast|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1360] [FILE:src/nexus_ai_agent/bot/handlers.py:940-964]|
|Real|/admin_logs|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1362] [FILE:src/nexus_ai_agent/bot/handlers.py:992-1004]|
|Real|/personality|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1370] [FILE:src/nexus_ai_agent/bot/handlers.py:1068-1093]|
|Real|/engagement_off|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1373] [FILE:src/nexus_ai_agent/bot/handlers.py:1112-1119]|
|Real|/engagement_on|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1372] [FILE:src/nexus_ai_agent/bot/handlers.py:1097-1110]|
|Real|/joke|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1375] [FILE:src/nexus_ai_agent/bot/handlers.py:1125-1127]|
|Real|/challenge|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1374] [FILE:src/nexus_ai_agent/bot/handlers.py:1121-1123]|
|Real|/analytics|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1403] [FILE:src/nexus_ai_agent/bot/handlers.py:1213-1238]|
|Real|/analytics_active|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1404] [FILE:src/nexus_ai_agent/bot/handlers.py:1240-1259]|
|Real|/analytics_retention|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1405] [FILE:src/nexus_ai_agent/bot/handlers.py:1261-1284]|
|Real|/track|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1406] [FILE:src/nexus_ai_agent/bot/handlers.py:1286-1298]|
|Real|/viral_now|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1378] [FILE:src/nexus_ai_agent/bot/handlers.py:1134-1143]|
|Real|/health|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1441] [FILE:src/nexus_ai_agent/bot/monitor_handlers.py:11-25]|
|Real|/agents|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1447] [FILE:src/nexus_ai_agent/bot/agent_handlers.py:9-39]|
|Real|/myagent|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1448] [FILE:src/nexus_ai_agent/bot/agent_handlers.py:71-83]|
|Real|/memory|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1451]|
|Real|/forget_me|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1452]|
|Real|/story|I/U|[FILE:src/nexus_ai_agent/bot/handlers.py:1474] [FILE:src/nexus_ai_agent/bot/handlers.py:1535-1565]|
|Simulated|/vision|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1412] [FILE:src/nexus_ai_agent/bot/handlers.py:376-385]|
|Simulated|/post|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1309] [FILE:tests/unit/test_surface_channel_management.py:1-481]|
|Simulated|/schedule|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1310] [FILE:tests/unit/test_surface_channel_management.py:1-481]|
|Simulated|/ban|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1311] [FILE:tests/unit/test_surface_channel_management.py:1-481]|
|Simulated|/unban|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1312] [FILE:tests/unit/test_surface_channel_management.py:1-481]|
|Simulated|/stats|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1313] [FILE:tests/unit/test_surface_channel_management.py:1-481]|
|Simulated|/welcome|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1314] [FILE:tests/unit/test_surface_channel_management.py:1-481]|
|Simulated|/pin|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1315] [FILE:tests/unit/test_surface_channel_management.py:1-481]|
|Simulated|/leaderboard|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1326] [FILE:src/nexus_ai_agent/bot/handlers.py:325-326]|
|Simulated|/daily|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1399] [FILE:tests/unit/test_surface_gamification.py:1-262]|
|Simulated|/xp_leaderboard|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1400] [FILE:tests/unit/test_surface_gamification.py:1-262]|
|Simulated|/achievements|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1401] [FILE:tests/unit/test_surface_gamification.py:1-262]|
|Simulated|/docs|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1466] [FILE:tests/unit/test_surface_docs.py:1-318]|
|Simulated|/doc_delete|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1467] [FILE:tests/unit/test_surface_docs.py:1-318]|
|Simulated|/chat_with_doc|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1468] [FILE:tests/unit/test_surface_docs.py:1-318]|
|Simulated|/newchat|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1431] [FILE:src/nexus_ai_agent/bot/handlers.py:829-834]|
|Simulated|/ad_create|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1383] [FILE:tests/unit/test_surface_ads.py:1-364]|
|Simulated|/ad_delete|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1387] [FILE:tests/unit/test_surface_ads.py:1-364]|
|Simulated|/ad_list|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1384] [FILE:tests/unit/test_surface_ads.py:1-364]|
|Simulated|/ad_pause|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1385] [FILE:tests/unit/test_surface_ads.py:1-364]|
|Simulated|/ad_resume|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1386] [FILE:tests/unit/test_surface_ads.py:1-364]|
|Simulated|/ad_stats|I/T|[FILE:src/nexus_ai_agent/bot/handlers.py:1388] [FILE:tests/unit/test_surface_ads.py:1-364]|
|Simulated|/mod_config|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1392] [FILE:src/nexus_ai_agent/bot/handlers.py:1187-1189]|
|Simulated|/warn|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1393] [FILE:src/nexus_ai_agent/bot/handlers.py:1191-1193]|
|Simulated|/mute|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1394] [FILE:src/nexus_ai_agent/bot/handlers.py:1195-1197]|
|Simulated|/unmute|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1395] [FILE:src/nexus_ai_agent/bot/handlers.py:1199-1201]|
|Simulated|/reputation|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1396] [FILE:src/nexus_ai_agent/bot/handlers.py:1203-1205]|
|Simulated|/viral_preview|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1379] [FILE:src/nexus_ai_agent/bot/handlers.py:1145-1147]|
|Simulated|/viral_stats|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1380] [FILE:src/nexus_ai_agent/bot/handlers.py:1149-1151]|
|Simulated|/viral_post|S|[FILE:src/nexus_ai_agent/bot/handlers.py:1381] [FILE:src/nexus_ai_agent/bot/handlers.py:1153-1155]|
|Simulated|/companion|M|No literal registration [FILE:src/nexus_ai_agent/bot/handlers.py:1300-1478]|
|Simulated|/analyze|M|No literal registration [FILE:src/nexus_ai_agent/bot/handlers.py:1300-1478]|
Counts: {'I/U': 48, 'P': 6, 'S': 11, 'I/T': 19, 'M': 2}. Status agreement: 59/86 = 68.6%. This is code-level status agreement, NOT % production functionality, test coverage, or all README prose verified. The 48 I/U entries must not be read as satisfying README’s stronger “working engine with tests” definition. Strict selected-test verification is 19/86 = 22.1% of these entries, under the deliberately conservative classification above.
