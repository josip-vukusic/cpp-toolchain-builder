// ANTLR undefines the C EOF macro. Keep its headers separate from consumers
// such as nlohmann/json that require EOF while parsing their own headers.
#include <antlr4-runtime.h>

bool test_antlr() {
  antlr4::ANTLRInputStream input("hello");
  return input.size() == 5;
}
