from crewai import Agent, Task, Crew, Process
from crewai.tools import BaseTool
from crewai_tools import SerperDevTool, ScrapeWebsiteTool
from pydantic import BaseModel, Field
from typing import Type
import os


# ── Custom Website Auditor Tool ───────────────────────────────────────────────

class _AuditInput(BaseModel):
    url: str = Field(description="Full website URL to audit, e.g. https://example.com")


class WebsiteAuditorTool(BaseTool):
    """
    CrewAI tool that wraps website_auditor.audit_website().
    Returns a structured text report with real PageSpeed scores,
    mobile performance, chatbot presence, outdated tech, and CTA quality.
    """

    name: str = "Website Performance Auditor"
    description: str = (
        "Runs a full technical audit on a company website. "
        "Returns real desktop & mobile performance scores (0-100), "
        "page load times (FCP, LCP), chatbot/live-chat detection, "
        "mobile-friendliness check, outdated technology scan, "
        "contact form detection, and CTA button analysis. "
        "ALWAYS call this tool for every lead — never skip or guess."
    )
    args_schema: Type[BaseModel] = _AuditInput
    pagespeed_api_key: str = ""

    def _run(self, url: str) -> str:
        from website_auditor import audit_website
        result = audit_website(url, self.pagespeed_api_key)
        return result.to_agent_summary()


# ── Crew Factory ──────────────────────────────────────────────────────────────

def create_lead_gen_crew(
    company_name: str,
    target_industry: str,
    your_service: str,
    your_name: str,
    lead_count: int = 5,
    target_country: str = "Worldwide (No restriction)",
    pagespeed_api_key: str = "",
):
    """
    Builds and returns a 3-agent CrewAI crew:
      Agent 1 — Lead Researcher   : finds companies with emails
      Agent 2 — Website Auditor   : runs real PageSpeed audits
      Agent 3 — Email Copywriter  : writes emails referencing specific audit data

    Returns (crew, task_research_leads, task_audit_websites)
    """

    search_tool = SerperDevTool()
    scrape_tool = ScrapeWebsiteTool()
    audit_tool = WebsiteAuditorTool(pagespeed_api_key=pagespeed_api_key)

    is_worldwide = target_country == "Worldwide (No restriction)"
    country_filter = "" if is_worldwide else f" based in {target_country}"
    location_rule = "" if is_worldwide else (
        f"\n\nSTRICT LOCATION RULE: ONLY include companies physically located in {target_country}. "
        f"Before adding any company, verify their address in the website footer, About page, "
        f"or Contact page. Immediately reject any company based in a different country."
    )

    # ── Agent 1: Lead Researcher ──────────────────────────────────────────────
    lead_researcher = Agent(
        role="B2B Lead Researcher",
        goal=(
            f"Find {lead_count} real, medium-sized companies{country_filter} in the "
            f"'{target_industry}' industry that are clearly successful businesses "
            f"(have customers, reviews, press coverage, or multiple locations) "
            f"but show visible signs of a weak or outdated digital presence. "
            f"For EVERY company you MUST find a real published email and phone number by "
            f"scraping the company's own website pages — 90%% of business websites publish "
            f"this information if you look in the right places. "
            f"NEVER guess or construct email addresses. "
            f"Only drop a company after exhausting all scraping steps below.{location_rule}"
        ),
        backstory=(
            "You are an expert B2B lead researcher and web scraper. You know that almost "
            "every business website publishes a contact email and phone number somewhere — "
            "in the page footer, on a /contact or /contact-us page, on an /about page, "
            "or embedded in the site's HTML as a mailto: or tel: link. "
            "Your process for EVERY company is: "
            "(1) Scrape the homepage — scan the footer and header for mailto: links, "
            "tel: links, and any visible email or phone text. "
            "(2) Scrape [website]/contact — this page almost always has the email and phone. "
            "(3) Scrape [website]/contact-us — try this if /contact returns nothing useful. "
            "(4) Scrape [website]/about or [website]/about-us — founders often list personal "
            "emails here. "
            "(5) Search Google for '[company name] email contact phone' — the top result "
            "often surfaces the contact info directly. "
            "(6) Check the company's LinkedIn page for a listed phone or email. "
            "Only after all 6 steps fail do you skip the company. "
            "You NEVER construct or guess patterns like info@, contact@, hello@, admin@."
        ),
        tools=[search_tool, scrape_tool],
        verbose=True,
        allow_delegation=False,
    )

    # ── Agent 2: Website Auditor ──────────────────────────────────────────────
    website_auditor_agent = Agent(
        role="Website Performance & UX Auditor",
        goal=(
            "For every company in the lead list, call the Website Performance Auditor tool "
            "with their website URL and collect the full audit report. "
            "You MUST call the tool for each company individually — never skip any and "
            "never make up or estimate scores. "
            "Report the exact scores, load times, issues list, and priority rating "
            "for each company so the email copywriter has real data to reference."
        ),
        backstory=(
            "You are a certified web performance engineer. You use real measurement tools "
            "to get precise scores — never guesses. A score of 34/100 on mobile is 10x "
            "more persuasive in an outreach email than 'your site looks slow'. "
            "Your audit reports are the foundation that makes outreach emails credible."
        ),
        tools=[audit_tool, scrape_tool],
        verbose=True,
        allow_delegation=False,
    )

    # ── Agent 3: Email Copywriter ─────────────────────────────────────────────
    email_writer = Agent(
        role="Personalized Cold Outreach Copywriter",
        goal=(
            f"Write one hyper-personalized cold outreach email per prospect. "
            f"Sender: {your_name} at {company_name}. Service offered: {your_service}. "
            f"Every email MUST reference SPECIFIC findings from the website audit — "
            f"use the exact score (e.g. 'your mobile score is 34/100'), name the exact problem "
            f"(e.g. 'no chatbot found', 'LCP is 6.1s'), and explain the business impact. "
            f"Then show in one sentence how {your_service} fixes that problem. "
            f"Keep every email under 160 words. No generic openers. Sound human, not salesy."
        ),
        backstory=(
            "You write cold emails that get replies because they reference real, verified data "
            "about the prospect's website — not vague claims. You never write 'I noticed your "
            "website could be improved'. You write 'your site scores 34/100 on mobile — "
            "that means most of your phone visitors leave before the page loads'. "
            "Your subject lines also reference specific numbers or problems to stand out."
        ),
        tools=[],
        verbose=True,
        allow_delegation=False,
    )

    # ── Task 1: Research Leads ────────────────────────────────────────────────
    task_research_leads = Task(
        description=(
            f"Find {lead_count} medium-sized, successful-looking companies{country_filter} "
            f"in the '{target_industry}' industry that would benefit from better web design, "
            f"speed optimisation, or AI/chatbot integration.\n\n"
            + (
                f"LOCATION FILTER: Every company MUST be physically based in {target_country}. "
                f"Verify their address before including them.\n\n"
                if not is_worldwide else ""
            )
            + "CONTACT INFO SCRAPING RULES — follow these steps for EVERY company before "
            "giving up on finding an email or phone:\n"
            "  Step 1: Scrape the company homepage — check the footer, header, and sidebar "
            "for mailto: links, tel: links, or visible email/phone text.\n"
            "  Step 2: Scrape [website]/contact — this page almost always has the email and phone.\n"
            "  Step 3: Scrape [website]/contact-us — try if /contact was empty.\n"
            "  Step 4: Scrape [website]/about or [website]/about-us — personal emails often appear here.\n"
            "  Step 5: Google search '[company name] contact email phone' and scrape the top result.\n"
            "  Step 6: Check the company LinkedIn page for listed contact details.\n"
            "Only skip a company if ALL 6 steps return nothing. "
            "NEVER construct or guess an email address (no info@, contact@, hello@, admin@).\n\n"
            "For each company provide ALL of the following:\n"
            "1. Company Name\n"
            "2. Website URL (full URL starting with https://)\n"
            "3. Location: City, Country\n"
            "4. Decision Maker: Name + Title (CEO / Founder / MD / Owner)\n"
            "5. Contact Email (address found via the scraping steps above — state which page/source it came from)\n"
            "6. Phone Number (found via scraping steps above — include country code)\n"
            "7. Success Signals: why this company looks successful\n"
            "8. Initial Website Notes: anything immediately visible that looks outdated or weak\n\n"
            "ONLY skip a company after completing all 6 scraping steps above."
        ),
        expected_output=(
            f"A numbered list of {lead_count} companies. Every entry must include "
            "all 8 fields above. The email MUST be one you actually found published online — "
            "never a constructed guess. No 'Not found' entries for email or website."
        ),
        agent=lead_researcher,
    )

    # ── Task 2: Audit Websites ────────────────────────────────────────────────
    task_audit_websites = Task(
        description=(
            "Take every company from the lead list. For EACH company, call the "
            "Website Performance Auditor tool using their website URL.\n\n"
            "RULES:\n"
            "- Call the tool for every single company — do NOT skip any\n"
            "- Do NOT estimate or make up scores — only use tool output\n"
            "- If the tool fails for a URL, try adding/removing 'www.' and retry once\n\n"
            "For each company, report:\n"
            "- Company Name\n"
            "- Desktop Performance Score (X/100)\n"
            "- Mobile Performance Score (X/100)\n"
            "- First Contentful Paint (FCP)\n"
            "- Largest Contentful Paint (LCP)\n"
            "- Chatbot / Live Chat: Found (name) or Not Found\n"
            "- Mobile Viewport: Present or Missing\n"
            "- Outdated Technology: list or None\n"
            "- Contact Form: Found or Not Found\n"
            "- CTA Buttons: Found or Not Found\n"
            "- Issues List: bullet points\n"
            "- Priority: HIGH / MEDIUM / LOW"
        ),
        expected_output=(
            f"Detailed audit report for all {lead_count} companies. Each entry clearly labeled "
            "with the company name, real tool-measured scores, and full issues list."
        ),
        agent=website_auditor_agent,
        context=[task_research_leads],
    )

    # ── Task 3: Write Emails ──────────────────────────────────────────────────
    task_write_emails = Task(
        description=(
            f"Write one personalized cold outreach email per prospect "
            f"using the lead list AND the website audit reports.\n\n"
            f"SENDER: {your_name}, {company_name}\n"
            f"SERVICE: {your_service}\n\n"
            "Every email MUST follow these rules:\n"
            "1. Subject line must mention the company's specific problem with a real number "
            "   (e.g. 'Your site scores 34/100 on mobile' or 'No chatbot = missed leads')\n"
            "2. Opening sentence must cite a SPECIFIC audit finding with the real score/data\n"
            "3. Second paragraph: explain the business cost of that problem\n"
            "4. Third paragraph: one sentence on how you fix it\n"
            "5. Closing: soft CTA — 15-min call or a simple reply\n"
            "6. Under 160 words total\n"
            "7. NEVER use generic phrases like 'I noticed your website could be improved'\n\n"
            "Format EACH email block exactly like this (include the --- separator):\n"
            "Email N — [Company Name]\n"
            "To: [contact email]\n"
            "Subject: [subject line]\n"
            "[email body]\n"
            "---"
        ),
        expected_output=(
            f"{lead_count} personalized cold emails. Each labeled 'Email N — Company Name', "
            "with To:, Subject:, and a body that references specific audit data and is under 160 words."
        ),
        agent=email_writer,
        context=[task_research_leads, task_audit_websites],
    )

    # ── Crew ──────────────────────────────────────────────────────────────────
    crew = Crew(
        agents=[lead_researcher, website_auditor_agent, email_writer],
        tasks=[task_research_leads, task_audit_websites, task_write_emails],
        process=Process.sequential,
        verbose=True,
    )

    return crew, task_research_leads, task_audit_websites
