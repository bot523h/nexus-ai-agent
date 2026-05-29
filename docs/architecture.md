# NEXUS AI v2.0.0 — Architecture Diagram

## System Overview

```mermaid
graph TB
    subgraph Telegram["Telegram Bot API"]
        TG[Telegram Users]
    end

    subgraph Handlers["Handler Layer (python-telegram-bot v21+)"]
        AUTH[Auth Middleware]
        RATE[Rate Limiter]
        FJ[Force Join Check]
        MOD[Moderation Pipeline]
        MENU[Inline Keyboard UI — v2.0.0 6-Section Menu]
    end

    subgraph Core["Core Orchestration (LangGraph)"]
        ROUTER[Intent Router]
        AGENT[Agent Node]
        MEMORY[Memory Node]
        TOOLS[Tools Node]
    end

    subgraph V2_AI["v2.0.0: Gemini AI Layer"]
        GEM[GeminiEngine — chat/ask/vision/code/translate]
        SUM[SummarizerEngine — 5 modes]
        IMG[ImageGenEngine — Pollinations.ai]
        SPCH[SpeechEngine — gTTS + Gemini STT]
    end

    subgraph V2_Cloud["v2.0.0: Unified Cloud Storage (57GB+)"]
        DBX[Dropbox 2GB]
        PCL[pCloud 10GB]
        INX[Internxt 10GB]
        MGA[MEGA 20GB]
        GHR[GitHub Releases]
        UCS[UnifiedCloudStorage — Round-RRobin Router]
    end

    subgraph V2_Growth["v2.0.0: Growth & i18n"]
        REF[ReferralEngine — 6 Viral Tiers]
        I18N[I18n — 15 Languages]
    end

    subgraph Features["Feature Engines (v1.x)"]
        OWN[Owner Control]
        PERS[Personality Engine]
        ENG[Engagement Engine]
        VIR[Viral Engine]
        ADS[Ad Manager]
        MD[Moderation Engine]
        GAM[Gamification Engine]
        ANA[Analytics Engine]
        CH[Channel Manager]
        ANON[Anonymous Chat]
        GAMES[Games]
        TOOL[Tools]
    end

    subgraph DB["SQLModel + SQLite (WAL)"]
        USERS[Users / Chats]
        ADMIN[AdminLog]
        FJC[ForceJoinConfig]
        PC[PersonalityConfig]
        EC[EngagementConfig]
        VP[ViralPost]
        AC[AdCampaign]
        MC[ModerationConfig]
        UR[UserReputation]
        XP[UserXP]
        AE[AnalyticsEvent]
        REF_TBL[Referral / ReferralCode]
        CF[CloudFile]
        UL[UserLanguage]
    end

    subgraph LLM["LLM Provider"]
        LLAMA[llama.cpp GGUF]
        FAKE[FakeLLMProvider]
    end

    subgraph External["External APIs (All Free)"]
        GEMINI[Google Gemini 2.0 Flash]
        POLL[Pollinations.ai]
        GTTS[gTTS]
    end

    TG --> AUTH
    AUTH --> RATE
    RATE --> FJ
    FJ --> MOD
    MOD --> MENU

    MENU --> ROUTER
    MENU --> Features
    MENU --> V2_AI
    MENU --> V2_Cloud
    MENU --> V2_Growth

    ROUTER --> AGENT
    AGENT --> MEMORY
    AGENT --> TOOLS
    AGENT --> LLM

    V2_AI --> External
    V2_Cloud --> DBX
    V2_Cloud --> PCL
    V2_Cloud --> INX
    V2_Cloud --> MGA
    V2_Cloud --> GHR
    UCS --> DBX & PCL & INX & MGA & GHR

    Features --> DB
    Core --> DB
    V2_AI --> DB
    V2_Cloud --> DB
    V2_Growth --> DB
```

## Data Flow — v2.0.0 AI Chat

```mermaid
sequenceDiagram
    participant U as User
    participant T as Telegram API
    participant H as Handler Layer
    participant GE as GeminiEngine
    participant G as Google Gemini API
    participant DB as SQLite

    U->>T: /ai Hello, how are you?
    T->>H: Update delivered
    H->>H: Auth + Rate Limit Check
    H->>GE: ai_cmd(text, conv_id, user_id)
    GE->>GE: Check rate limits (15 RPM, 1500/day)
    GE->>GE: Load conversation history (up to 20 msgs)
    GE->>G: POST /v1beta/models/gemini-2.0-flash:generateContent
    G-->>GE: AI response text
    GE->>GE: Append to conversation history
    GE->>DB: Persist conversation state
    GE-->>H: Response text
    H->>T: Send reply
    T->>U: Message delivered
```

## Data Flow — v2.0.0 Cloud Upload

```mermaid
sequenceDiagram
    participant U as User
    participant T as Telegram API
    participant H as Handler Layer
    participant UCS as UnifiedCloudStorage
    participant DB as SQLite
    participant P as Cloud Provider (Dropbox/pCloud/Internxt/MEGA/GitHub)

    U->>T: Reply to file → /cloud
    T->>H: Update with document
    H->>H: Download file bytes
    H->>H: Save to temp file
    H->>UCS: upload_file(local_path, remote_key)
    UCS->>UCS: Select provider (round-robin + capacity)
    UCS->>P: Upload file
    P-->>UCS: Upload result + provider info
    UCS-->>H: {success, provider, remote_path}
    H->>DB: Save CloudFile record (user_id, file_name, provider, size)
    H->>H: Delete temp file
    H->>T: Confirmation message
    T->>U: ☁️ Uploaded! 📁 file.txt 📦 Dropbox 📊 45.2KB
```

## Data Flow — v2.0.0 Referral System

```mermaid
sequenceDiagram
    participant A as User A (Referrer)
    participant B as User B (New User)
    participant T as Telegram API
    participant H as Handler Layer
    participant RE as ReferralEngine
    participant DB as SQLite

    A->>T: /referral
    T->>H: Command
    H->>RE: format_stats(user_id)
    RE->>DB: Get/create referral code
    RE->>DB: Get referral stats
    RE-->>H: Formatted stats with link
    H->>T: Your code: NEXUS-123-ABC, Link: t.me/bot?start=ref_NEXUS-123-ABC
    T->>A: Referral info

    B->>T: Click referral link → /start ref_NEXUS-123-ABC
    T->>H: Start with start_param
    H->>RE: process_referral(B_id, start_param)
    RE->>RE: Validate code, check not self-referral
    RE->>DB: Create Referral record
    RE->>DB: Increment referrer's successful_referrals
    RE->>RE: Check tier upgrade
    RE-->>H: {success, referrer_id, tier_info}
    H->>T: Welcome! You've been referred 🎉
    T->>B: Welcome message

    H->>T: Notify referrer
    T->>A: 🔗 New referral! You've reached 🥈 Networker tier!
```

## Feature Dependencies — v2.0.0

```mermaid
graph LR
    subgraph V1["v1.x Features"]
        OC[Owner Control]
        FJ[Force Join]
        PE[Personality]
        EE[Engagement]
        VE[Viral Engine]
        AM[Ad Manager]
        ME[Moderation]
        GE[Gamification]
        AE[Analytics]
        UI_v1[Advanced UI]
    end

    subgraph V2["v2.0.0 Features"]
        GEMINI[GeminiEngine]
        IMG[ImageGenEngine]
        SPCH[SpeechEngine]
        SUM[SummarizerEngine]
        UCS[UnifiedCloudStorage]
        REF[ReferralEngine]
        I18N[I18n]
    end

    OC --> VE
    OC --> AM
    OC --> ME
    PE --> EE
    ME --> GE
    GE --> AE
    AE --> UI_v1

    GEMINI --> SPCH
    GEMINI --> SUM
    REF --> I18N
    UCS --> REF
```

## Database Schema — v2.0.0

```mermaid
erDiagram
    User ||--o{ UserXP : has
    User ||--o{ UserReputation : has
    User ||--o{ AnalyticsEvent : generates
    User ||--o{ ReferralCode : has
    User ||--o{ UserLanguage : prefers
    User ||--o{ CloudFile : owns
    Chat ||--o{ ForceJoinConfig : configured_in
    Chat ||--o{ PersonalityConfig : configured_in
    Chat ||--o{ EngagementConfig : configured_in
    Chat ||--o{ ModerationConfig : configured_in
    Chat ||--o{ AdCampaign : contains
    Chat ||--o{ ViralPost : contains
    Chat ||--o{ AnalyticsEvent : tracks
    ReferralCode ||--o{ Referral : generates

    User {
        int id PK
        int telegram_id
        string username
        bool is_allowed
    }

    ReferralCode {
        int id PK
        int user_id FK
        string code UK
        int total_referrals
        int successful_referrals
    }

    Referral {
        int id PK
        int referrer_id FK
        int referee_id FK_UK
        string referral_code
        string status
        bool reward_claimed
    }

    UserLanguage {
        int id PK
        int user_id FK_UK
        string language
    }

    CloudFile {
        int id PK
        int user_id FK
        string file_name
        string provider
        string remote_path
        int file_size
    }
```

## v2.0.0 API Rate Limits

| API | Free Tier | Rate Limit | Daily Limit |
|-----|-----------|------------|-------------|
| Google Gemini 2.0 Flash | Free | 15 RPM | 1,500 requests/day |
| Pollinations.ai | Free | Unlimited | Unlimited |
| gTTS | Free | Unlimited | Unlimited |
| Dropbox API | Free (2GB) | 100 requests/min | — |
| pCloud API | Free (10GB) | — | — |
| Internxt API | Free (10GB) | — | — |

## v2.0.0 Referral Reward Tiers

| Tier | Title | Referrals | Reward | XP |
|------|-------|-----------|--------|-----|
| 🥉 | Inviter | 1 | Referral Badge | 50 |
| 🥈 | Networker | 3 | 3 Days Premium | 150 |
| 🥇 | Star | 5 | 7 Days Premium | 300 |
| 💎 | Diamond | 10 | 30 Days Premium + Unlimited AI | 500 |
| 👑 | Legendary | 25 | VIP Lifetime + Special Badge | 1,000 |
| 🚀 | Viral Master | 50 | Co-Owner + All Features | 2,500 |
