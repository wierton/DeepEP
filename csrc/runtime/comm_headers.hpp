#pragma once
#include <algorithm>
#include <fstream>
#include <iterator>
#include <filesystem>
#include <vector>
#include <deep_jit/utils/hash.hpp>
#include <deep_jit/utils/exception.hpp>

namespace deep_ep {
// DeepJIT only parses project-style angle-bracket includes. Installed comm
// headers use relative quoted includes: hash their contents without rewriting
// them or copying them into this package.
inline std::string comm_header_signature(const std::filesystem::path& asc_root) {
    std::vector<std::filesystem::path> files;
    for (const auto* subtree : {"include/comm_api/aicore", "impl/comm_api/aicore"}) {
        for (const auto& file : std::filesystem::recursive_directory_iterator(asc_root / subtree)) {
            if (file.is_regular_file() && (file.path().extension() == ".h" || file.path().extension() == ".hpp"))
                files.push_back(file.path().lexically_relative(asc_root));
        }
    }
    std::sort(files.begin(), files.end());
    deep_jit::hash::FNV1a hash;
    for (const auto& file : files) {
        std::ifstream input(asc_root / file, std::ios::binary);
        DJ_HOST_ASSERT(input.is_open(), "Cannot read installed communication header: {}", file.string());
        const std::string content{std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
        hash.update(file.generic_string()).update(content);
    }
    return hash.get_hex_digest();
}
} // namespace deep_ep
