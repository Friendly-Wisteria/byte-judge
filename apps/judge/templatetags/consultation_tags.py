"""相談先の案内を、テンプレートから呼べるようにする。

500.html は server_error から context も request も無しで render されるため、
コンテキストプロセッサは動かない。タグなら context に依存せずに渡せる。
"""

from django import template

from ..consultation import CONSULTATION_CONTACTS, CONSULTATION_HEADING

register = template.Library()


@register.inclusion_tag("judge/_consultation_guide.html")
def consultation_guide():
    return {"heading": CONSULTATION_HEADING, "contacts": CONSULTATION_CONTACTS}
