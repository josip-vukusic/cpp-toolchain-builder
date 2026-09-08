#include <google/protobuf/any.pb.h>
#include <re2/re2.h>
#include <fmt/format.h>
#include <spdlog/spdlog.h>
#include <toml++/toml.hpp>
#include <concurrentqueue.h>
#include <nlohmann/json.hpp>
#include <zlib.h>
#include <zstd.h>
#include <lz4.h>
#include <lzma.h>
#include <bzlib.h>
#include <modbus/modbus.h>
#include <gflags/gflags.h>
#include <iostream>

bool test_antlr();

int main() {
  google::protobuf::Any message;
  message.set_type_url("type.googleapis.com/Test");
  std::string encoded;
  if (!message.SerializeToString(&encoded) || encoded.empty()) return 1;
  if (!RE2::FullMatch("42", "[0-9]+")) return 2;
  if (!test_antlr()) return 3;
  auto config = toml::parse("answer = 42");
  if (config["answer"].value_or(0) != 42) return 4;
  moodycamel::ConcurrentQueue<int> queue;
  int answer = 0;
  queue.enqueue(42);
  if (!queue.try_dequeue(answer) || answer != 42) return 5;
  nlohmann::json json{{"answer", answer}};
  if (json.at("answer") != 42 || fmt::format("{}", answer) != "42") return 6;
  spdlog::set_level(spdlog::level::off);
  spdlog::info("consumer test");
  if (!zlibVersion() || ZSTD_versionNumber() == 0 || LZ4_versionNumber() == 0 ||
      !lzma_version_string() || !BZ2_bzlibVersion()) return 7;
  modbus_t* connection = modbus_new_tcp("127.0.0.1", 502);
  if (!connection) return 8;
  modbus_free(connection); // No network connection is attempted.
  gflags::SetUsageMessage("toolchain consumer");
  std::cout << "All 16 recipe components linked and ran successfully.\n";
}
