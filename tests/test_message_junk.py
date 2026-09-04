"""Цитата требования не должна быть обрывком служебных данных.

Найдено на реально отправленных письмах: восемь из семидесяти девяти ушли
рекрутёру с куском сериализованного словаря вместо требования вакансии —

    В требованиях увидел «{'education': 'Высшее образование — специалитет, ма…

Такое письмо не просто выглядит небрежно: оно сообщает получателю, что с ним
разговаривает сломанный автомат. Второй попытки написать в ту же компанию не
будет, поэтому проверка стоит на входе в цитату, а не на выходе.
"""
from jobhunter.match.scorer import score_job
from jobhunter.profile import get_profile
from jobhunter.tailor.message import _extract_requirement, generate

JD_WITH_DICT = """Python-разработчик
Требования: {'education': 'Высшее образование — специалитет, магистратура',
'experience': '3 года', 'schedule': null}
Опыт разработки на Python, знание FastAPI и PostgreSQL обязательно.
"""


def _score(jd):
    return score_job("Python-разработчик", "", jd)


def test_serialized_dict_never_becomes_a_quote():
    score = _score(JD_WITH_DICT)
    req = _extract_requirement(JD_WITH_DICT, score.matched_skills,
                               get_profile(), "Python-разработчик")
    assert "{'" not in req and '{"' not in req
    assert "education" not in req


def test_json_fragments_are_rejected():
    for line in ("{\"title\": \"Senior\", \"remote\": true}",
                 "'level': 'middle', 'stack': 'python'",
                 "[{'skill': 'Python'}]"):
        jd = "Python-разработчик\n%s\nОпыт работы с Python и Docker.\n" % line
        req = _extract_requirement(jd, _score(jd).matched_skills, get_profile(), "")
        assert "'" not in req and '"' not in req, "просочилось: %r" % req


def test_letter_stays_clean_on_dirty_input():
    score = _score(JD_WITH_DICT)
    msg = generate("Python-разработчик", JD_WITH_DICT, score,
                   seed_str="dirty", source="ваш пост в @pyjobs")
    assert "{'" not in msg.text
    assert "null" not in msg.text
