# DRAFT — not filed. Target: https://gitlab.freedesktop.org/mesa/mesa/-/issues (label: KosmicKrisp)

**Title:** kosmickrisp: SPIR-V integer ops emitted as signed `int` in MSL, so wrap-around is UB and gets miscompiled (`b == -b` at 0x80000000)

Searched the Mesa tracker for "kosmickrisp overflow/signed/ineg/miscompil"; found no existing report.

**Setup:** Vulkan SDK 1.4.363.0 KosmicKrisp (driverInfo `vulkan-sdk-1.4.363 (git-269a955c89)`), Apple M4 Pro, macOS 26.6.2.

**Repro** (SPIR-V compute shader; shown as Slang source)
```slang
// all operands unsigned; b = (0x80000000, 0, 1, 0x7FFFFFFF) read from a buffer
outp[0] = uint4(b == -b);   // expected (1, 1, 0, 0); KosmicKrisp returns (0, 1, 0, 0)
```
MoltenVK and native Metal return the expected values. When the zero is only known at runtime (`b != (a - b)` with `a = 0`), KosmicKrisp is correct, so this looks like a compile-time simplification.

**Cause (from MESA_KK_DEBUG=msl)**
```
t28 = *(coherent device int*)(t27);
t47 = -t28;
t51 = t28 == t47;
```
NIR/SPIR-V integer negation wraps, but in MSL negating `INT_MIN` as an `int` is undefined (C++ rules). That allows Apple's compiler to fold `x == -x` to `x == 0`. The fold is inferred from the result, which matches it exactly. `nir_to_msl.c` emits `nir_op_ineg` as `-` and `iadd/isub/imul` as `+ - *`, and `msl_type_inference.c` can type these as `TYPE_INT`.

**Possible fix:** emit wrapping integer ops on unsigned types (e.g. `as_type<int>(as_type<uint>(a) + as_type<uint>(b))`), or infer `TYPE_UINT` for wrapping arithmetic.
