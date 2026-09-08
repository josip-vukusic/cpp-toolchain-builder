#include <fmt/format.h>
#include <spdlog/spdlog.h>

int main() {
    spdlog::set_pattern("%v");
    spdlog::info("{}", fmt::format("The answer is {}", 42));
}
