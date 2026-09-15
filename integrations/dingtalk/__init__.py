"""钉钉读写适配器（T03 / T03.1）。

按冻结的公共契约实现 ReadPort、WritePort、StagePort。传输由调用方注入
（测试假传输或 `DwsTransport` 直调 dws.js）。本包不保存凭据、不猜测安装路径。
解析层只接受公开 T01 已观察形态；写入不明时先回查，不盲重发。
"""
