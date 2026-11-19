from jobbot.qa import Ctx, choose_option, resolve

A = {"Full Name": "Asha Rao", "Email": "v@x.com", "Phone": "+91 1", "LinkedIn": "https://l/in/v",
     "Notice Period": "7 days (maximum)", "Current CTC": "6 LPA", "Expected CTC": "10 LPA",
     "Current Company": "Acme", "Current Title": "PA", "Current Location": "Pune",
     "How did you hear about us": "LinkedIn"}
C = Ctx(A, 2.6, [("pronoun", "He/Him")])


def test_identity():
    assert resolve("First Name *", "text", C) == "Asha"
    assert resolve("Last name", "text", C) == "Rao"
    assert resolve("Email", "email", C) == "v@x.com"
    assert resolve("Phone", "tel", C) == "+91 1"


def test_ctc_and_exp():
    assert resolve("Current CTC", "text", C) == "6 LPA"
    assert resolve("Expected CTC (in LPA)", "number", C) == "10"
    assert resolve("Expected salary", "number", C) is None
    assert resolve("Total years of experience", "number", C) == "2"
    assert resolve("Total experience", "text", C) == "2.5"


def test_rules_first_and_unknown():
    assert resolve("Preferred pronouns", "text", C) == "He/Him"
    assert resolve("Why do you want to join us?", "text", C) is None


def test_options():
    assert choose_option("Notice period", "7 days (maximum)", ["Select", "Immediate", "30 days"]) == "Immediate"
    assert choose_option("Source", "LinkedIn", ["Select...", "LinkedIn", "Referral"]) == "LinkedIn"
    assert choose_option("x", "Maybe", ["Yes", "No"]) is None


def test_skill_experience_not_guessed():
    assert resolve("Years of experience with Kubernetes", "number", C) is None


def test_bank_match_and_unknown_logging(tmp_path):
    from jobbot.qa import load_bank
    f = tmp_path / "a.yaml"
    f.write_text('"Are you legally authorized to work in India?": "Yes"\n"Blank q": ""\n', encoding="utf-8")
    bank, known = load_bank(f)
    c = Ctx(A, 2.6, [], bank, f, known)
    assert resolve("Are you legally authorized to work in India? *", "text", c) == "Yes"
    assert resolve("Blank q", "text", c) is None
    c.note("Why do you want this job?")
    c.note("Blank q")                         # already in file: not added again
    assert c.flush_unknown() == 1
    text = f.read_text(encoding="utf-8")
    assert text.count("Blank q") == 1 and '"Why do you want this job?": ""' in text


def test_standing_answers():
    assert resolve("Are you willing to relocate?", "text", C) == "Yes"
    assert resolve("Will you now or in the future require visa sponsorship?", "text", C) == "No"
