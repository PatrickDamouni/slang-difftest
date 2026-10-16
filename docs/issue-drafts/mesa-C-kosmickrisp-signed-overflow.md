# DRAFT — not filed. Target: https://gitlab.freedesktop.org/mesa/mesa/-/issues (label: KosmicKrisp)

**Title:** kosmickrisp: integer arithmetic emitted as signed `int` in MSL, so overflow is UB and gets miscompiled (`x == -x`, `x + 1 > x`)

**Setup:** Vulkan SDK 1.4.363.0 KosmicKrisp (driverInfo `vulkan-sdk-1.4.363 (git-269a955c89)`), Apple M4 Pro, macOS 26.6.2.

**Repro** (SPIR-V compute shader; shown as Slang source)
```slang
outp[0] = uint4(b == -b);                         // b.x = 0x80000000 -> expected 1, KosmicKrisp 0
outp[1] = uint4(int4(b) + int4(1,1,1,1) > int4(b)); // b.x = 0x7FFFFFFF -> expected 0, KosmicKrisp 1
```
MoltenVK and native Metal return the expected values. When the zero is only known at runtime (`b != (a - b)` with `a = 0`), KosmicKrisp is correct, so this looks like a compile-time simplification.

**Cause (from MESA_KK_DEBUG=msl)**
```
t28 = *(coherent device int*)(t27);
t47 = -t28;
t51 = t28 == t47;
```
NIR/SPIR-V integer arithmetic wraps, but in MSL signed overflow is undefined (C++ rules). That lets Apple's compiler rewrite `x == -x` to `x == 0` and `x + 1 > x` to `true`. `nir_to_msl.c` emits `nir_op_ineg` as `-` and `iadd/isub/imul` as `+ - *`, and `msl_type_inference.c` can type these as `TYPE_INT`.

**Possible fix:** emit wrapping integer ops on unsigned types (e.g. `as_type<int>(as_type<uint>(a) + as_type<uint>(b))`), or infer `TYPE_UINT` for wrapping arithmetic.
