"""Английские ответы рекрутёров: дыры, найденные аудитом, закрыты.

До этих паттернов «we went with another candidate» без слова unfortunately
проходило мимо классификатора, и заявка вечно ждала ответа; «take-home
assignment» не считался техвопросом; «are you free tomorrow at 2pm» не
распознавался как предложение времени.
"""
from jobhunter.convo.classify import classify


def test_en_rejections():
    for txt in ("We went with another candidate for this role.",
                "We regret to inform you that we chose someone else.",
                "The position has been filled.",
                "We decided to move forward with another applicant.",
                "We will not be moving forward with your application."):
        assert classify(txt).label == "rejection", txt


def test_en_tech_questions_escalate():
    for txt in ("Could you complete a take-home assignment?",
                "We'd like you to do a coding challenge.",
                "How would you design a rate limiter?",
                "Please walk me through your last project."):
        it = classify(txt)
        assert it.needs_human, "%s -> %s" % (txt, it.label)


def test_rate_limiter_is_not_money():
    assert classify("How would you design a rate limiter?").label != "money"
    assert classify("What is your hourly rate?").label == "money"
    assert classify("What are your rate expectations?").label == "money"


def test_en_call_and_slots():
    assert classify("Let's book a call this week.").label in ("ask_call",
                                                             "slot_proposed")
    assert classify("Are you free for a quick chat?").label == "ask_call"
    it = classify("Would tomorrow at 2pm work for you?")
    assert it.label in ("slot_proposed", "ask_call")


def test_ru_regression():
    assert classify("К сожалению, мы выбрали другого кандидата.").label == "rejection"
    assert classify("Пришлите, пожалуйста, резюме.").label == "ask_cv"
    assert classify("Когда вам удобно созвониться?").label == "ask_call"
