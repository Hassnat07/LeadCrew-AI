# ⚡ LeadCrew AI — Lead Generation & Outreach Agent

A production-ready **multi-agent AI system** built with **CrewAI** that automatically:
1.  **Finds 5 qualified leads** in your target industry
2.  **Researches each company** — pain points, recent news, website analysis
3.  **Writes personalized cold emails** — under 150 words, specific to each prospect

Built as a **portfolio project** to showcase on Upwork.

---

##  Architecture

```
User Input
    │
    ▼
┌─────────────────────────────────────────────┐
│              CrewAI Orchestrator             │
│                                             │
│  Agent 1: Lead Researcher                   │
│  ├── Tool: SerperDevTool (web search)        │
│  └── Output: 5 leads with contacts          │
│                    │                        │
│  Agent 2: Company Analyst                   │
│  ├── Tool: ScrapeWebsiteTool                │
│  ├── Tool: SerperDevTool                    │
│  └── Output: Intel brief per company        │
│                    │                        │
│  Agent 3: Email Copywriter                  │
│  └── Output: 5 personalized cold emails     │
└─────────────────────────────────────────────┘
    │
    ▼
Streamlit UI (results + download)
```

---

##  Setup & Run

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get API Keys (both free tiers available)
- **OpenAI**: https://platform.openai.com → API Keys
- **Serper**: 557e4ee0eb5388f07e54333d8b236cb7d3e04ed2

### 3. Run the app
```bash
streamlit run app.py
```

### 4. Fill in the form
- Your name + company
- Target industry (e.g. "SaaS startups", "e-commerce brands")
- Your service (e.g. "AI automation", "web development")
- Paste API keys in the sidebar
- Click **Launch Agent Crew** 

---

##  Project Structure

```
lead-gen-agent/
├── app.py           # Streamlit UI
├── agents.py        # CrewAI agents + tasks + crew
├── requirements.txt # Dependencies
└── README.md        # This file
```

---

##  How to Use This on Upwork

1. Record a **Loom demo video** showing the agents running
2. Show the before (manual lead gen) vs after (automated)
3. Offer it as a service: **"I'll build you a custom AI lead generation system"**
4. Typical Upwork rate: **$300–$800** per project

---

##  Customization Ideas for Clients

- Add LinkedIn scraping via Apify
- Export to Google Sheets or HubSpot CRM
- Add email sending via SendGrid
- Add a scoring agent that ranks leads by fit
- Add industry-specific templates (SaaS, real estate, agencies)

---

## 🛠️ Tech Stack

| Layer | Tool |
|-------|------|
| Agent Framework | CrewAI |
| LLM | OpenAI GPT-4o / GPT-4o-mini |
| Web Search | Serper API |
| Web Scraping | BeautifulSoup + CrewAI scrape tool |
| UI | Streamlit |
| Language | Python 3.10+ |

---

*Built with  as a CrewAI portfolio project*
