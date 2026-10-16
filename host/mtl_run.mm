// Generic native-Metal compute runner for slang-difftest.
//
//   mtl_run <kernel.metal> <kernel_name> <in_index> <out_index> <threads> <in.bin> <out.bin> <out_words>
//
// Compiles the MSL that slangc generated at runtime (newLibraryWithSource),
// with fast math explicitly OFF so floating-point reassociation can never
// cause a false mismatch, binds the input/output words at the buffer indices
// slangc chose (parsed by run.py from the kernel signature), dispatches
// ceil(threads / 64) threadgroups of 64, and writes the output as raw uint32.
// Exit codes: 0 ok, 2 usage, 3 runtime failure, 4 Metal rejected the MSL.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

int main(int argc, char** argv) {
    @autoreleasepool {
        if (argc != 9) {
            std::fprintf(stderr, "usage: mtl_run kernel.metal name in_idx out_idx threads in.bin out.bin out_words\n");
            return 2;
        }
        NSError* err = nil;
        NSString* src = [NSString stringWithContentsOfFile:@(argv[1]) encoding:NSUTF8StringEncoding error:&err];
        if (!src) {
            std::fprintf(stderr, "cannot read %s\n", argv[1]);
            return 2;
        }
        NSString* name = @(argv[2]);
        const NSUInteger in_idx = std::strtoul(argv[3], nullptr, 10);
        const NSUInteger out_idx = std::strtoul(argv[4], nullptr, 10);
        const NSUInteger threads = std::strtoul(argv[5], nullptr, 10);
        NSData* input = [NSData dataWithContentsOfFile:@(argv[6])];
        const char* out_path = argv[7];
        const NSUInteger out_words = std::strtoul(argv[8], nullptr, 10);
        if (!input || threads == 0 || out_words == 0) return 2;

        id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
        MTLCompileOptions* opts = [MTLCompileOptions new];
        opts.mathMode = MTLMathModeSafe;  // no fast-math reassociation
        id<MTLLibrary> lib = [dev newLibraryWithSource:src options:opts error:&err];
        if (!lib) {
            std::fprintf(stderr, "METAL_COMPILE_ERROR: %s\n", err.localizedDescription.UTF8String);
            return 4;
        }
        id<MTLFunction> fn = [lib newFunctionWithName:name];
        if (!fn) {
            std::fprintf(stderr, "no function %s\n", argv[2]);
            return 3;
        }
        id<MTLComputePipelineState> pso = [dev newComputePipelineStateWithFunction:fn error:&err];
        if (!pso) {
            std::fprintf(stderr, "METAL_PIPELINE_ERROR: %s\n", err.localizedDescription.UTF8String);
            return 4;
        }
        id<MTLBuffer> in = [dev newBufferWithBytes:input.bytes length:input.length options:MTLResourceStorageModeShared];
        id<MTLBuffer> out = [dev newBufferWithLength:out_words * 4 options:MTLResourceStorageModeShared];
        std::memset(out.contents, 0xAB, out_words * 4);  // poison unwritten words

        id<MTLCommandQueue> q = [dev newCommandQueue];
        id<MTLCommandBuffer> cb = [q commandBuffer];
        id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:pso];
        [enc setBuffer:in offset:0 atIndex:in_idx];
        [enc setBuffer:out offset:0 atIndex:out_idx];
        [enc dispatchThreadgroups:MTLSizeMake((threads + 63) / 64, 1, 1) threadsPerThreadgroup:MTLSizeMake(64, 1, 1)];
        [enc endEncoding];
        [cb commit];
        [cb waitUntilCompleted];
        if (cb.status != MTLCommandBufferStatusCompleted) {
            std::fprintf(stderr, "command buffer failed: %s\n", cb.error.localizedDescription.UTF8String);
            return 3;
        }
        std::FILE* f = std::fopen(out_path, "wb");
        if (!f || std::fwrite(out.contents, 1, out_words * 4, f) != out_words * 4) return 3;
        std::fclose(f);
    }
    return 0;
}
