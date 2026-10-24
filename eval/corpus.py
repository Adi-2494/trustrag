"""
Evaluation corpus: four fictional company policy documents with KNOWN facts on KNOWN pages,
plus a question set derived from those facts. Because the ground truth is written here, every
answer, citation and retrieval result can be checked automatically.

`python -m eval.corpus` writes the PDFs to eval/corpus/ and the questions to eval/data/qa.jsonl.
"""

import json
import os

# Each document: list of pages; each page: (heading, [sentences]).
DOCS = {
    "hr_handbook.pdf": [
        (
            "Welcome and working hours",
            [
                "Brightleaf Robotics has offices in Pune, Berlin and Toronto.",
                "Core working hours are 10:00 to 16:00, Monday to Friday.",
                "Outside core hours, employees may start and finish when they choose, provided they work 40 hours per week.",
                "Employees must record their hours in the TimeTrack portal by 18:00 every Friday.",
            ],
        ),
        (
            "Probation and notice",
            [
                "New employees serve a probation period of 6 months.",
                "During probation, either side may end employment with 2 weeks' notice.",
                "After probation, the notice period is 2 months for individual contributors and 3 months for managers.",
            ],
        ),
        (
            "Annual and sick leave",
            [
                "Full-time employees receive 25 days of paid annual leave per year, in addition to public holidays.",
                "Up to 5 unused annual leave days may be carried over, and carried-over days expire on 31 March.",
                "Employees receive 12 days of paid sick leave per year.",
                "A doctor's note is needed for absences longer than 3 consecutive working days.",
            ],
        ),
        (
            "Family leave",
            [
                "Birthing parents receive 26 weeks of fully paid parental leave.",
                "Non-birthing parents receive 6 weeks of fully paid parental leave, to be taken within the child's first year.",
                "Employees may take up to 5 days of paid compassionate leave after the death of a close family member.",
            ],
        ),
        (
            "Benefits",
            [
                "The company matches pension contributions up to 6% of base salary.",
                "Each employee has an annual learning budget of 1,500 EUR for courses, books and conferences.",
                "The wellness allowance is 50 EUR per month and can be used for gym memberships or fitness classes.",
            ],
        ),
        (
            "Remote work and conduct",
            [
                "Employees may work remotely up to 3 days per week with their manager's agreement.",
                "A one-time home office allowance of 400 EUR is paid during the first month of employment.",
                "Gifts from suppliers worth more than 75 EUR must be declared to the compliance team.",
            ],
        ),
    ],
    "it_security_policy.pdf": [
        (
            "Passwords and accounts",
            [
                "Passwords must be at least 14 characters long.",
                "Passwords only need to be changed if a compromise is suspected, and routine rotation is not required.",
                "Accounts are locked after 10 failed sign-in attempts.",
            ],
        ),
        (
            "Multi-factor authentication and devices",
            [
                "Multi-factor authentication is mandatory for email, VPN and the HR portal.",
                "Laptops must use full-disk encryption and lock the screen after 5 minutes of inactivity.",
                "Personal phones may access company email only through the approved Intune app.",
            ],
        ),
        (
            "Reporting incidents",
            [
                "Suspected security incidents must be reported to the security team within 1 hour of discovery.",
                "Lost or stolen devices must be reported to the IT helpdesk on extension 4400.",
                "Phishing emails should be reported using the Report Phish button in Outlook.",
            ],
        ),
        (
            "Data classification",
            [
                "Data is classified as Public, Internal, Confidential or Restricted.",
                "Restricted data, such as salary information, may only be stored on the encrypted HR drive.",
                "Confidential documents must not be shared outside the company without approval from the data owner.",
            ],
        ),
        (
            "Using AI tools",
            [
                "Employees may use only the approved internal AI assistant for company data.",
                "Confidential or Restricted data must never be pasted into public AI chatbots.",
                "Developers who build AI features must keep system prompts in version control and review them like code.",
                "Documents that contain hidden instructions aimed at AI tools must be reported to the security team.",
            ],
        ),
    ],
    "travel_expense_policy.pdf": [
        (
            "Booking travel",
            [
                "All business travel must be booked through the TravelDesk portal at least 10 days before departure.",
                "Trips abroad require approval from the department head.",
            ],
        ),
        (
            "Flights and trains",
            [
                "Economy class is required for flights under 5 hours.",
                "Premium economy may be booked for flights of 5 hours or more.",
                "Train travel is preferred over flying for journeys under 4 hours.",
            ],
        ),
        (
            "Hotels and meals",
            [
                "Hotel costs are reimbursed up to 180 EUR per night in capital cities and 130 EUR elsewhere.",
                "The daily meal allowance is 45 EUR within Europe and 60 EUR outside Europe.",
                "Alcohol is not reimbursed.",
            ],
        ),
        (
            "Mileage and expense claims",
            [
                "Use of a private car is reimbursed at 0.30 EUR per kilometre.",
                "Expense claims must be submitted within 60 days of the expense, with receipts.",
                "Claims above 2,000 EUR require approval from the finance director.",
            ],
        ),
    ],
    # Distractor document: same topics as the HR handbook, deliberately different numbers.
    "intern_policy.pdf": [
        (
            "Interns: hours and probation",
            [
                "Interns work 30 hours per week, scheduled with their mentor.",
                "Internships have a probation period of 1 month.",
                "Interns may end the internship with 1 week's notice.",
            ],
        ),
        (
            "Interns: leave and remote work",
            [
                "Interns receive 10 days of paid annual leave per year, pro rata for shorter internships.",
                "Interns may work remotely 1 day per week.",
                "Interns receive 5 days of paid sick leave.",
            ],
        ),
        (
            "Interns: learning and pay",
            [
                "Each intern has a learning budget of 300 EUR.",
                "Interns are paid a monthly stipend on the last working day of the month.",
            ],
        ),
    ],
    "contractor_policy.pdf": [
        (
            "Scope and time off",
            [
                "This policy applies to independent contractors, not to employees.",
                "Contractors do not receive paid annual leave.",
                "Unpaid time off must be agreed with the engagement manager 2 weeks in advance.",
            ],
        ),
        (
            "Invoicing and equipment",
            [
                "Contractors invoice monthly, by the 5th working day of the following month.",
                "Invoices are paid within 30 days of receipt.",
                "Contractors must use their own equipment unless a company laptop is required for security reasons.",
            ],
        ),
        (
            "Ending an engagement",
            [
                "Either side may end a contractor engagement with 30 days' written notice.",
                "Contractors must return all company property and access badges on their last day.",
            ],
        ),
    ],
}

# (question, file, page, evidence sentence or part of it, answer key groups)
# An answer is correct if, for EVERY group, it contains AT LEAST ONE of the group's strings (case-insensitive).
FACTS = [
    (
        "Where does Brightleaf Robotics have offices?",
        "hr_handbook.pdf",
        1,
        "offices in Pune, Berlin and Toronto",
        [["pune"], ["berlin"], ["toronto"]],
    ),
    (
        "What are the core working hours?",
        "hr_handbook.pdf",
        1,
        "Core working hours are 10:00 to 16:00",
        [["10:00", "10 am", "10am"], ["16:00", "4 pm", "4pm"]],
    ),
    (
        "How many hours per week must employees work?",
        "hr_handbook.pdf",
        1,
        "provided they work 40 hours per week",
        [["40"]],
    ),
    (
        "By when must hours be recorded in TimeTrack?",
        "hr_handbook.pdf",
        1,
        "by 18:00 every Friday",
        [["18:00", "6 pm", "6pm"], ["friday"]],
    ),
    (
        "How long is the probation period for new employees?",
        "hr_handbook.pdf",
        2,
        "probation period of 6 months",
        [["6 months", "six months"]],
    ),
    (
        "What is the notice period during probation?",
        "hr_handbook.pdf",
        2,
        "with 2 weeks' notice",
        [["2 weeks", "two weeks"]],
    ),
    (
        "What is the notice period for managers after probation?",
        "hr_handbook.pdf",
        2,
        "3 months for managers",
        [["3 months", "three months"]],
    ),
    (
        "How many days of paid annual leave do full-time employees get?",
        "hr_handbook.pdf",
        3,
        "25 days of paid annual leave",
        [["25"]],
    ),
    (
        "How many unused annual leave days can be carried over?",
        "hr_handbook.pdf",
        3,
        "Up to 5 unused annual leave days",
        [["5", "five"]],
    ),
    ("When do carried-over leave days expire?", "hr_handbook.pdf", 3, "expire on 31 March", [["31 march", "march 31"]]),
    (
        "How many paid sick days do employees get per year?",
        "hr_handbook.pdf",
        3,
        "12 days of paid sick leave",
        [["12"]],
    ),
    (
        "When is a doctor's note needed for sick leave?",
        "hr_handbook.pdf",
        3,
        "longer than 3 consecutive working days",
        [["3", "three"]],
    ),
    (
        "How much parental leave do birthing parents get?",
        "hr_handbook.pdf",
        4,
        "26 weeks of fully paid parental leave",
        [["26"]],
    ),
    (
        "How much parental leave do non-birthing parents get?",
        "hr_handbook.pdf",
        4,
        "6 weeks of fully paid parental leave",
        [["6 weeks", "six weeks"]],
    ),
    (
        "How many days of compassionate leave can employees take?",
        "hr_handbook.pdf",
        4,
        "up to 5 days of paid compassionate leave",
        [["5", "five"]],
    ),
    (
        "Up to what percentage does the company match pension contributions?",
        "hr_handbook.pdf",
        5,
        "up to 6% of base salary",
        [["6%", "6 %", "six percent"]],
    ),
    ("What is the annual learning budget?", "hr_handbook.pdf", 5, "learning budget of 1,500 EUR", [["1,500", "1500"]]),
    (
        "How much is the monthly wellness allowance?",
        "hr_handbook.pdf",
        5,
        "wellness allowance is 50 EUR per month",
        [["50"]],
    ),
    (
        "How many days per week can employees work remotely?",
        "hr_handbook.pdf",
        6,
        "up to 3 days per week",
        [["3", "three"]],
    ),
    ("How much is the home office allowance?", "hr_handbook.pdf", 6, "home office allowance of 400 EUR", [["400"]]),
    ("Above what value must supplier gifts be declared?", "hr_handbook.pdf", 6, "worth more than 75 EUR", [["75"]]),
    ("What is the minimum password length?", "it_security_policy.pdf", 1, "at least 14 characters long", [["14"]]),
    (
        "Do passwords need to be rotated regularly?",
        "it_security_policy.pdf",
        1,
        "routine rotation is not required",
        [["not required", "no", "only"]],
    ),
    (
        "After how many failed sign-in attempts is an account locked?",
        "it_security_policy.pdf",
        1,
        "locked after 10 failed sign-in attempts",
        [["10", "ten"]],
    ),
    (
        "Which systems require multi-factor authentication?",
        "it_security_policy.pdf",
        2,
        "mandatory for email, VPN and the HR portal",
        [["email"], ["vpn"], ["hr portal"]],
    ),
    (
        "After how many minutes of inactivity must laptops lock the screen?",
        "it_security_policy.pdf",
        2,
        "after 5 minutes of inactivity",
        [["5", "five"]],
    ),
    (
        "How can personal phones access company email?",
        "it_security_policy.pdf",
        2,
        "only through the approved Intune app",
        [["intune"]],
    ),
    (
        "How quickly must suspected security incidents be reported?",
        "it_security_policy.pdf",
        3,
        "within 1 hour of discovery",
        [["1 hour", "one hour"]],
    ),
    (
        "Which extension should I call to report a lost laptop?",
        "it_security_policy.pdf",
        3,
        "extension 4400",
        [["4400"]],
    ),
    ("How should phishing emails be reported?", "it_security_policy.pdf", 3, "Report Phish button", [["report phish"]]),
    (
        "What are the data classification levels?",
        "it_security_policy.pdf",
        4,
        "Public, Internal, Confidential or Restricted",
        [["public"], ["internal"], ["confidential"], ["restricted"]],
    ),
    (
        "Where may restricted data such as salaries be stored?",
        "it_security_policy.pdf",
        4,
        "only be stored on the encrypted HR drive",
        [["hr drive"]],
    ),
    (
        "Who must approve sharing confidential documents outside the company?",
        "it_security_policy.pdf",
        4,
        "approval from the data owner",
        [["data owner"]],
    ),
    (
        "Which AI assistant may be used for company data?",
        "it_security_policy.pdf",
        5,
        "approved internal AI assistant",
        [["internal"]],
    ),
    (
        "Where must developers keep system prompts?",
        "it_security_policy.pdf",
        5,
        "keep system prompts in version control",
        [["version control"]],
    ),
    (
        "How far in advance must business travel be booked?",
        "travel_expense_policy.pdf",
        1,
        "at least 10 days before departure",
        [["10", "ten"]],
    ),
    (
        "Who must approve trips abroad?",
        "travel_expense_policy.pdf",
        1,
        "approval from the department head",
        [["department head"]],
    ),
    (
        "Which class must be booked for flights under 5 hours?",
        "travel_expense_policy.pdf",
        2,
        "Economy class is required for flights under 5 hours",
        [["economy"]],
    ),
    (
        "When may premium economy be booked?",
        "travel_expense_policy.pdf",
        2,
        "flights of 5 hours or more",
        [["5 hours", "five hours"]],
    ),
    (
        "When is train travel preferred over flying?",
        "travel_expense_policy.pdf",
        2,
        "journeys under 4 hours",
        [["4 hours", "four hours"]],
    ),
    (
        "What is the hotel limit per night in capital cities?",
        "travel_expense_policy.pdf",
        3,
        "180 EUR per night in capital cities",
        [["180"]],
    ),
    (
        "What is the daily meal allowance outside Europe?",
        "travel_expense_policy.pdf",
        3,
        "60 EUR outside Europe",
        [["60"]],
    ),
    ("Is alcohol reimbursed?", "travel_expense_policy.pdf", 3, "Alcohol is not reimbursed", [["not", "no"]]),
    (
        "What is the mileage rate for using a private car?",
        "travel_expense_policy.pdf",
        4,
        "0.30 EUR per kilometre",
        [["0.30", "0,30", "30 cent"]],
    ),
    (
        "Within how many days must expense claims be submitted?",
        "travel_expense_policy.pdf",
        4,
        "within 60 days of the expense",
        [["60"]],
    ),
    (
        "Who must approve expense claims above 2,000 EUR?",
        "travel_expense_policy.pdf",
        4,
        "approval from the finance director",
        [["finance director"]],
    ),
    # Distractors: similar topics to the HR handbook, different answers.
    (
        "Do contractors receive paid annual leave?",
        "contractor_policy.pdf",
        1,
        "Contractors do not receive paid annual leave",
        [["not", "no "]],
    ),
    (
        "How far in advance must contractors agree unpaid time off?",
        "contractor_policy.pdf",
        1,
        "engagement manager 2 weeks in advance",
        [["2 weeks", "two weeks"]],
    ),
    (
        "By when must contractors send their monthly invoice?",
        "contractor_policy.pdf",
        2,
        "by the 5th working day of the following month",
        [["5th", "fifth"]],
    ),
    (
        "Within how many days are contractor invoices paid?",
        "contractor_policy.pdf",
        2,
        "paid within 30 days of receipt",
        [["30"]],
    ),
    (
        "What notice is needed to end a contractor engagement?",
        "contractor_policy.pdf",
        3,
        "30 days' written notice",
        [["30"]],
    ),
    (
        "How many days of paid annual leave do interns get?",
        "intern_policy.pdf",
        2,
        "Interns receive 10 days of paid annual leave",
        [["10", "ten"]],
    ),
    (
        "How many days per week can interns work remotely?",
        "intern_policy.pdf",
        2,
        "Interns may work remotely 1 day per week",
        [["1 day", "one day"]],
    ),
    (
        "How long is the probation period for interns?",
        "intern_policy.pdf",
        1,
        "probation period of 1 month",
        [["1 month", "one month"]],
    ),
    ("What is the learning budget for interns?", "intern_policy.pdf", 3, "learning budget of 300 EUR", [["300"]]),
    ("How many hours per week do interns work?", "intern_policy.pdf", 1, "Interns work 30 hours per week", [["30"]]),
]

# Ordinary policy prose (no facts that any question asks about), mixed into every page so pages are realistic
# in length and split into several chunks - otherwise retrieval would be trivially easy.
FILLER = [
    "This section applies to all locations unless local law requires otherwise, in which case local law takes precedence.",
    "Questions about this section should first be raised with your line manager, who can involve the relevant team if needed.",
    "Exceptions may be granted in special circumstances, but they must be documented and approved in writing before they take effect.",
    "The company reviews this policy regularly and announces material changes on the intranet and in the monthly newsletter.",
    "Where this policy refers to managers, it also includes team leads who have formal people-management responsibilities.",
    "Employees are expected to use good judgement and to act in the spirit of this policy, not only the letter.",
    "Records created under this policy are kept in line with the company's data retention schedule.",
    "Breaches of this policy are handled through the normal disciplinary process, which is described in a separate document.",
    "Some teams publish additional local guidelines; where they conflict with this policy, this policy applies.",
    "Requests are normally answered within a reasonable time, and urgent cases can be escalated to the people team.",
    "The intranet contains templates, frequently asked questions and contact details related to this section.",
    "New joiners are introduced to this policy during onboarding and are asked to confirm that they have read it.",
]


def _filler_for(file_name: str, page: int) -> list[str]:
    # Deterministic choice, so the corpus (and therefore the results) is identical on every run.
    start = (sum(map(ord, file_name)) + page * 5) % len(FILLER)
    return [FILLER[(start + i) % len(FILLER)] for i in range(5)]


UNANSWERABLE = [
    "What is the name of the CEO?",
    "What was the company's revenue last year?",
    "How many employees work in the Toronto office?",
    "Is dental insurance included in the benefits?",
    "What is the office dress code?",
    "What percentage is the annual bonus?",
    "How much is the monthly internet allowance?",
    "Which VPN software does the company use?",
    "What is the daily allowance for interns?",
    "Are pets allowed in the office?",
    "When is the company holiday party?",
    "What is the maximum budget for a new laptop?",
    "Which airline should employees prefer?",
    "What is the Wi-Fi password?",
    "In which year was the company founded?",
]

ROOT = os.path.dirname(__file__)
CORPUS_DIR = os.path.join(ROOT, "corpus")
QA_FILE = os.path.join(ROOT, "data", "qa.jsonl")


def page_paragraphs(file_name: str, page: int) -> list[str]:
    """The page as paragraphs: filler before and after the facts, so facts sit in realistic surroundings."""
    _, sentences = DOCS[file_name][page - 1]
    filler = _filler_for(file_name, page)
    return [" ".join(filler[:3]), " ".join(sentences), " ".join(filler[3:])]


def page_text(file_name: str, page: int) -> str:
    heading, _ = DOCS[file_name][page - 1]
    return heading + "\n" + "\n".join(page_paragraphs(file_name, page))


def build() -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    # Every evidence string must really be on its page, or the ground truth would be wrong.
    for question, file_name, page, evidence, _ in FACTS:
        assert evidence in page_text(file_name, page), f"evidence not on page: {question}"

    os.makedirs(CORPUS_DIR, exist_ok=True)
    styles = getSampleStyleSheet()
    for file_name, pages in DOCS.items():
        title = file_name.replace(".pdf", "").replace("_", " ").title()
        story = []
        for number, (heading, _sentences) in enumerate(pages, start=1):
            if number == 1:
                story.append(Paragraph(f"Brightleaf Robotics - {title} (fictional)", styles["Title"]))
            story.append(Paragraph(heading, styles["Heading2"]))
            for paragraph in page_paragraphs(file_name, number):
                story.append(Paragraph(paragraph, styles["BodyText"]))
            if number < len(pages):
                story.append(PageBreak())
        SimpleDocTemplate(os.path.join(CORPUS_DIR, file_name), pagesize=A4).build(story)

    os.makedirs(os.path.dirname(QA_FILE), exist_ok=True)
    with open(QA_FILE, "w", encoding="utf-8") as f:
        for i, (question, file_name, page, evidence, keys) in enumerate(FACTS):
            f.write(
                json.dumps(
                    {
                        "id": f"q{i:03d}",
                        "question": question,
                        "answerable": True,
                        "file": file_name,
                        "page": page,
                        "evidence": evidence,
                        "answer_keys": keys,
                    }
                )
                + "\n"
            )
        for i, question in enumerate(UNANSWERABLE):
            f.write(json.dumps({"id": f"u{i:03d}", "question": question, "answerable": False}) + "\n")
    print(f"Wrote {len(DOCS)} PDFs to {CORPUS_DIR} and {len(FACTS) + len(UNANSWERABLE)} questions to {QA_FILE}")


if __name__ == "__main__":
    build()
