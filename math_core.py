"""Exact arithmetic; no eval and no language model in grading."""
from dataclasses import dataclass
from fractions import Fraction
from math import isqrt
import re


@dataclass(frozen=True)
class Task:
    a: int
    b: int
    c: int

    @property
    def d(self):
        return self.b * self.b - 4 * self.a * self.c

    @property
    def roots(self):
        if self.d < 0:
            return ()
        s = isqrt(self.d)
        assert s * s == self.d, 'Bank uses rational roots only'
        return tuple(sorted({Fraction(-self.b-s, 2*self.a),
                             Fraction(-self.b+s, 2*self.a)}))

    @property
    def equation(self):
        terms = []
        for n, suffix in ((self.a, 'x²'), (self.b, 'x'), (self.c, '')):
            if not n:
                continue
            value = ('' if abs(n) == 1 and suffix else str(abs(n))) + suffix
            terms.append(('− ' if n < 0 else '+ ') + value)
        return ' '.join(terms).removeprefix('+ ').replace('− ', '−', 1) + ' = 0'

    def hint(self, level):
        hints = [
            f'Визнач коефіцієнти разом зі знаками: a = {self.a}, b = {self.b}, c = {self.c}. Обчисли D = b² − 4ac.',
            f'D = ({self.b})² − 4·({self.a})·({self.c}) = {self.d}. Порівняй D з нулем: скільки дійсних коренів?',
            ('D < 0: дійсних коренів немає. Введи: немає.' if self.d < 0 else
             f'Скористайся x = (−b ± √D)/(2a). Тут −b = {-self.b}, √D = {isqrt(self.d)}, 2a = {2*self.a}. Обчисли та перевір підстановкою.')
        ]
        return hints[min(level, 2)]

    def solution(self):
        answer = '; '.join(map(str, self.roots)) or 'дійсних коренів немає'
        return f'a = {self.a}, b = {self.b}, c = {self.c}.\nD = {self.d}.\nВідповідь: {answer}.'


# Six matched TYPES per assessment, distinct coefficients. Pilot equivalence
# must be checked by the research supervisor; it is not a validated instrument.
BANKS = {
    'pre': [Task(1,-5,6), Task(2,-3,-2), Task(1,-6,9), Task(1,0,-16), Task(3,-6,0), Task(1,2,5)],
    'post': [Task(1,-7,12), Task(2,-5,-3), Task(1,-8,16), Task(1,0,-25), Task(3,-9,0), Task(1,4,8)],
    'delayed': [Task(1,-9,20), Task(2,-7,-4), Task(1,-10,25), Task(1,0,-36), Task(3,-12,0), Task(1,6,13)],
    'training': [Task(1,-3,2), Task(2,1,-3), Task(1,-4,4), Task(1,0,-9), Task(2,-10,0), Task(1,2,3),
                 Task(1,1,-6), Task(3,-4,1), Task(1,6,9), Task(4,0,-1), Task(4,8,0), Task(2,2,5)],
}


def parse_answer(text):
    """Use semicolon/space for roots; comma is decimal separator. Exact match."""
    text = text.strip().lower().replace('−', '-').replace('–', '-')
    if text in {'немає', 'нема', 'немає коренів', 'дійсних коренів немає', '∅'}:
        return ()
    if len(text) > 100:
        raise ValueError('Задовга відповідь.')
    parts = re.split(r'[;\s]+', text)
    if not 1 <= len(parts) <= 2:
        raise ValueError('Введи один або два корені; роздільник — крапка з комою.')
    values = []
    for p in parts:
        p = p.replace(',', '.')
        if not re.fullmatch(r'[+-]?\d{1,8}(?:\.\d{1,8}|/[+-]?\d{1,8})?', p):
            raise ValueError('Приклад: 2; -1/2. Десяткова кома дозволена: 0,5.')
        try:
            if '/' in p:
                numerator, denominator = p.split('/')
                values.append(Fraction(int(numerator), int(denominator)))
            else:
                values.append(Fraction(p))
        except (ValueError, ZeroDivisionError):
            raise ValueError('Перевір запис дробу: знаменник не може бути нулем.') from None
    return tuple(sorted(set(values)))


def error_kind(answer, task):
    if answer == task.roots:
        return 'correct'
    return 'root_count' if len(answer) != len(task.roots) else 'root_values'
