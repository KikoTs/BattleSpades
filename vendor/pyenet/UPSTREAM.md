# Vendored pyenet and ENet

BattleSpades builds its `enet` Python extension directly from these pinned
sources so every native release target uses the same transport implementation.

- pyenet tag `1.3.17`, commit `1bd4e84b4d6bcfb171c11572a1b7b770123e3771`
- ENet tag `v1.3.17`, commit `e0e7045b7e056b454b5093cb34df49dc4cee0bee`
- pyenet source: <https://github.com/piqueserver/pyenet>
- ENet source: <https://github.com/lsalzman/enet>

Wrapper-source modifications are limited to the explicit Cython
`language_level=2` directive retained from pyenet's upstream build script and
a `noexcept` annotation on ENet's packet-free callback required by Cython 3.
pyenet is BSD-3-Clause licensed; ENet is MIT licensed. Their complete license
texts are stored beside their respective sources.
