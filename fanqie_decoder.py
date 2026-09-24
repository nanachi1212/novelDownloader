"""Conservative decoder for the two observed Fanqie private-use code tables.

Table source: https://github.com/ying-ck/fanqienovel-downloader/blob/main/src/charset.json
The source has 372 mode-0 entries and 371 mode-1 entries; the missing last
mode-1 position is explicitly unknown here, never guessed.
"""

from dataclasses import dataclass

PUA_STARTS = (0xE3E8, 0xE3E9)
CHARSETS = (
    (
        'D在主特家军然表场4要只v和?6别还g现儿岁??此象月3出战工相o男直失世F都平文什VO将真T那当?会立些u是十张学气大爱两'
        '命全后东性通被1它乐接而感车山公了常以何可话先pi叫轻M士w着变尔快l个说少色里安花远7难师放t报认面道S?克地度I好机U民'
        '写把万同水新没书电吃像斯5为y白几日教看但第加候作上拉住有法r事应位利你声身国问马女他Y比父xAHNsX边美对所金活回意到z'
        '从j知又内因点Q三定8Rb正或夫向德听更?得告并本q过记L让打f人就者去原满体做经K走如孩cG给使物?最笑部?员等受k行一条'
        '果动光门头见往自解成处天能于名其发总母的死手入路进心来h时力多开已许d至由很界n小与Z想代么分生口再妈望次西风种带J?实情才'
        '这?E我神格长觉间年眼无不亲关结0友信下却重己老2音字m呢明之前高PB目太e9起稜她也W用方子英每理便四数期中C外样a海们任'
    ),
    (
        's?作口在他能并B士4U克才正们字声高全尔活者动其主报多望放hw次年?中3特于十入要男同G面分方K什再教本己结1等世N?说g'
        'u期Z外美M行给9文将两许张友0英应向像此白安少何打气常定间花见孩它直风数使道第水已女山解dP的通关性叫儿L妈问回神来S?四'
        '望前国些OvlA心平自无军光代是好却c得种就意先立z子过Yj表?么所接了名金受J满眼没部那m每车度可R斯经现门明V如走命y6'
        'E战很上f月西7长夫想话变海机x到W一成生信笑但父开内东马日小而后带以三几为认X死员目位之学远人音呢我q乐象重对个被别F也书'
        '稜D写还因家发时i或住德当ol比觉然吃去公a老亲情体太b万C电理?失力更拉物着原她工实色感记看出相路大你候2和?与p样新只便'
        '最不进Tr做格母总爱身师轻知往加从?天eH?听场由快边让把任8条头事至起点真手这难都界用法n处下又Q告地5kt岁有会果利民?'
    ),
)


class DecodeFailed(RuntimeError):
    """DECODE_FAILED: private-use characters could not be restored confidently."""


@dataclass(frozen=True)
class DecodeResult:
    text: str
    mode: int | None
    pua_count: int


def _is_pua(char: str) -> bool:
    return 0xE000 <= ord(char) <= 0xF8FF


def decode_pua(text: str, mode: int) -> str:
    """Replace only known PUA positions; keep all unknown/ordinary codepoints."""
    if mode not in (0, 1):
        raise ValueError("mode must be 0 or 1")
    start = PUA_STARTS[mode]
    table = CHARSETS[mode]
    output = []
    for char in text:
        index = ord(char) - start
        output.append(table[index] if 0 <= index < len(table) and table[index] != "?" else char)
    return "".join(output)


def decode_chapter(text: str, preferred_mode: int | None = None) -> DecodeResult:
    """Select a mode only when its PUA coverage and Han ratio are unambiguous."""
    pua = [char for char in text if _is_pua(char)]
    if not pua:
        return DecodeResult(text, None, 0)
    if len(pua) < 20 and preferred_mode not in (0, 1):
        raise DecodeFailed("DECODE_FAILED: too little PUA text to select a mapping mode")
    candidates = []
    for mode in (0, 1):
        mapped = decode_pua("".join(pua), mode)
        unknown = sum(_is_pua(char) for char in mapped)
        han = sum(0x3400 <= ord(char) <= 0x9FFF for char in mapped)
        latin = sum(char.isascii() and char.isalpha() for char in mapped)
        han_ratio = han / len(pua)
        if unknown == 0 and han_ratio >= 0.90 and latin / len(pua) <= 0.03:
            candidates.append((mode, han_ratio))
    if preferred_mode in (0, 1) and len(pua) < 20:
        candidates = [entry for entry in candidates if entry[0] == preferred_mode]
    if len(candidates) == 1:
        mode = candidates[0][0]
    elif len(candidates) == 2 and abs(candidates[0][1] - candidates[1][1]) >= 0.08:
        mode = max(candidates, key=lambda entry: entry[1])[0]
    else:
        raise DecodeFailed("DECODE_FAILED: the PUA mapping mode is ambiguous or incomplete")
    return DecodeResult(decode_pua(text, mode), mode, len(pua))
