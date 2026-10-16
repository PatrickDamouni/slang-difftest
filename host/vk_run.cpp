// Generic Vulkan compute runner for slang-difftest.
//
//   vk_run <kernel.spv> <threads> <in.bin> <out.bin> <out_words>
//
// Binds set 0: binding 0 = input words (read-only storage buffer),
// binding 1 = output words. Dispatches ceil(threads / 64) groups of the SPIR-V
// entry point "main", then writes the output buffer as raw little-endian
// uint32. The driver (MoltenVK or KosmicKrisp) is picked by the Vulkan loader
// from VK_DRIVER_FILES. Exit codes: 0 ok, 2 usage, 3 driver/runtime failure.
#include <vulkan/vulkan.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <vector>

#define VK_CHECK(expr)                                                                     \
    do {                                                                                   \
        VkResult r_ = (expr);                                                              \
        if (r_ != VK_SUCCESS) {                                                            \
            std::fprintf(stderr, "%s failed: VkResult %d (line %d)\n", #expr, r_, __LINE__); \
            std::exit(3);                                                                  \
        }                                                                                  \
    } while (0)

namespace {

constexpr uint32_t kLocalSize = 64;  // matches [numthreads(64,1,1)] in the generated kernel

std::vector<char> read_file(const char* path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        std::fprintf(stderr, "cannot open %s\n", path);
        std::exit(2);
    }
    return {std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()};
}

bool has_ext(const std::vector<VkExtensionProperties>& exts, const char* name) {
    for (const auto& e : exts)
        if (std::strcmp(e.extensionName, name) == 0) return true;
    return false;
}

struct Buf {
    VkBuffer buf = VK_NULL_HANDLE;
    VkDeviceMemory mem = VK_NULL_HANDLE;
    void* map = nullptr;
};

}  // namespace

int main(int argc, char** argv) {
    if (argc != 6) {
        std::fprintf(stderr, "usage: vk_run kernel.spv threads in.bin out.bin out_words\n");
        return 2;
    }
    const std::vector<char> spv = read_file(argv[1]);
    const uint32_t threads = static_cast<uint32_t>(std::strtoul(argv[2], nullptr, 10));
    const std::vector<char> input = read_file(argv[3]);
    const char* out_path = argv[4];
    const size_t out_words = std::strtoul(argv[5], nullptr, 10);
    if (spv.size() % 4 != 0 || input.empty() || out_words == 0 || threads == 0) {
        std::fprintf(stderr, "bad inputs\n");
        return 2;
    }

    // Instance: opt into portability enumeration only if offered (MoltenVK).
    uint32_t n = 0;
    vkEnumerateInstanceExtensionProperties(nullptr, &n, nullptr);
    std::vector<VkExtensionProperties> iexts(n);
    vkEnumerateInstanceExtensionProperties(nullptr, &n, iexts.data());
    std::vector<const char*> ienabled;
    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    if (has_ext(iexts, VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME)) {
        ienabled.push_back(VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME);
        ici.flags |= VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR;
    }
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.apiVersion = VK_API_VERSION_1_2;
    ici.pApplicationInfo = &app;
    ici.enabledExtensionCount = static_cast<uint32_t>(ienabled.size());
    ici.ppEnabledExtensionNames = ienabled.data();
    VkInstance inst;
    VK_CHECK(vkCreateInstance(&ici, nullptr, &inst));

    VK_CHECK(vkEnumeratePhysicalDevices(inst, &n, nullptr));
    if (n == 0) {
        std::fprintf(stderr, "no Vulkan device\n");
        return 3;
    }
    std::vector<VkPhysicalDevice> pds(n);
    VK_CHECK(vkEnumeratePhysicalDevices(inst, &n, pds.data()));
    VkPhysicalDevice pd = pds[0];

    uint32_t qn = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, nullptr);
    std::vector<VkQueueFamilyProperties> qp(qn);
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, qp.data());
    uint32_t qf = UINT32_MAX;
    for (uint32_t i = 0; i < qn; ++i)
        if (qp[i].queueFlags & VK_QUEUE_COMPUTE_BIT) { qf = i; break; }
    if (qf == UINT32_MAX) return 3;

    vkEnumerateDeviceExtensionProperties(pd, nullptr, &n, nullptr);
    std::vector<VkExtensionProperties> dexts(n);
    vkEnumerateDeviceExtensionProperties(pd, nullptr, &n, dexts.data());
    std::vector<const char*> denabled;
    if (has_ext(dexts, "VK_KHR_portability_subset")) denabled.push_back("VK_KHR_portability_subset");

    const float prio = 1.0f;
    VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qci.queueFamilyIndex = qf;
    qci.queueCount = 1;
    qci.pQueuePriorities = &prio;
    VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = static_cast<uint32_t>(denabled.size());
    dci.ppEnabledExtensionNames = denabled.data();
    VkDevice dev;
    VK_CHECK(vkCreateDevice(pd, &dci, nullptr, &dev));
    VkQueue queue;
    vkGetDeviceQueue(dev, qf, 0, &queue);

    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(pd, &mp);
    auto make = [&](VkDeviceSize bytes) {
        Buf b;
        VkBufferCreateInfo bci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
        bci.size = bytes;
        bci.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
        VK_CHECK(vkCreateBuffer(dev, &bci, nullptr, &b.buf));
        VkMemoryRequirements req;
        vkGetBufferMemoryRequirements(dev, b.buf, &req);
        const VkMemoryPropertyFlags want =
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        uint32_t type = UINT32_MAX;
        for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
            if ((req.memoryTypeBits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) {
                type = i;
                break;
            }
        if (type == UINT32_MAX) std::exit(3);
        VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        mai.allocationSize = req.size;
        mai.memoryTypeIndex = type;
        VK_CHECK(vkAllocateMemory(dev, &mai, nullptr, &b.mem));
        VK_CHECK(vkBindBufferMemory(dev, b.buf, b.mem, 0));
        VK_CHECK(vkMapMemory(dev, b.mem, 0, bytes, 0, &b.map));
        return b;
    };
    Buf in = make(input.size());
    std::memcpy(in.map, input.data(), input.size());
    const VkDeviceSize out_bytes = out_words * 4;
    Buf out = make(out_bytes);
    std::memset(out.map, 0xAB, out_bytes);  // poison: unwritten words show up as 0xABABABAB

    VkDescriptorSetLayoutBinding binds[2] = {};
    for (uint32_t i = 0; i < 2; ++i) {
        binds[i].binding = i;
        binds[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        binds[i].descriptorCount = 1;
        binds[i].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    }
    VkDescriptorSetLayoutCreateInfo dl{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    dl.bindingCount = 2;
    dl.pBindings = binds;
    VkDescriptorSetLayout setl;
    VK_CHECK(vkCreateDescriptorSetLayout(dev, &dl, nullptr, &setl));
    VkPipelineLayoutCreateInfo pl{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    pl.setLayoutCount = 1;
    pl.pSetLayouts = &setl;
    VkPipelineLayout plo;
    VK_CHECK(vkCreatePipelineLayout(dev, &pl, nullptr, &plo));

    VkShaderModuleCreateInfo smci{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    smci.codeSize = spv.size();
    smci.pCode = reinterpret_cast<const uint32_t*>(spv.data());
    VkShaderModule sm;
    VK_CHECK(vkCreateShaderModule(dev, &smci, nullptr, &sm));
    VkComputePipelineCreateInfo cpci{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    cpci.stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    cpci.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    cpci.stage.module = sm;
    cpci.stage.pName = "main";
    cpci.layout = plo;
    VkPipeline pipe;
    VK_CHECK(vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &cpci, nullptr, &pipe));

    VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2};
    VkDescriptorPoolCreateInfo dpci{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    dpci.maxSets = 1;
    dpci.poolSizeCount = 1;
    dpci.pPoolSizes = &ps;
    VkDescriptorPool pool;
    VK_CHECK(vkCreateDescriptorPool(dev, &dpci, nullptr, &pool));
    VkDescriptorSetAllocateInfo dsai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    dsai.descriptorPool = pool;
    dsai.descriptorSetCount = 1;
    dsai.pSetLayouts = &setl;
    VkDescriptorSet set;
    VK_CHECK(vkAllocateDescriptorSets(dev, &dsai, &set));
    VkDescriptorBufferInfo infos[2] = {{in.buf, 0, VK_WHOLE_SIZE}, {out.buf, 0, VK_WHOLE_SIZE}};
    VkWriteDescriptorSet w[2] = {};
    for (uint32_t i = 0; i < 2; ++i) {
        w[i].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        w[i].dstSet = set;
        w[i].dstBinding = i;
        w[i].descriptorCount = 1;
        w[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        w[i].pBufferInfo = &infos[i];
    }
    vkUpdateDescriptorSets(dev, 2, w, 0, nullptr);

    VkCommandPoolCreateInfo cpi{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    cpi.queueFamilyIndex = qf;
    VkCommandPool cpool;
    VK_CHECK(vkCreateCommandPool(dev, &cpi, nullptr, &cpool));
    VkCommandBufferAllocateInfo cbai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cbai.commandPool = cpool;
    cbai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbai.commandBufferCount = 1;
    VkCommandBuffer cb;
    VK_CHECK(vkAllocateCommandBuffers(dev, &cbai, &cb));
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    VK_CHECK(vkBeginCommandBuffer(cb, &bi));
    vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipe);
    vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, plo, 0, 1, &set, 0, nullptr);
    vkCmdDispatch(cb, (threads + kLocalSize - 1) / kLocalSize, 1, 1);
    VkMemoryBarrier mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
    mb.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
    mb.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
    vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1,
                         &mb, 0, nullptr, 0, nullptr);
    VK_CHECK(vkEndCommandBuffer(cb));
    VkFenceCreateInfo fci{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VkFence fence;
    VK_CHECK(vkCreateFence(dev, &fci, nullptr, &fence));
    VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.commandBufferCount = 1;
    si.pCommandBuffers = &cb;
    VK_CHECK(vkQueueSubmit(queue, 1, &si, fence));
    VK_CHECK(vkWaitForFences(dev, 1, &fence, VK_TRUE, 10ull * 1000 * 1000 * 1000));

    std::FILE* f = std::fopen(out_path, "wb");
    if (!f || std::fwrite(out.map, 1, out_bytes, f) != out_bytes) return 3;
    std::fclose(f);
    // Process exit releases the device; this runner is one-shot by design so a
    // driver crash or hang only takes down this process (see run.py timeouts).
    return 0;
}
