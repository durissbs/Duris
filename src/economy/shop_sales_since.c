#include "economy/shop_sales_since.h"

#include "flatfile/flatfile_store.h"

#include <cerrno>
#include <cstdint>
#include <cstring>
#include <sys/stat.h>
#include <vector>

namespace
{
constexpr const char *since_file = "since";
}

time_t shop_sales_since(const std::string &directory, bool copyover, time_t now, std::string *error)
{
	if (copyover)
	{
		std::vector<uint8_t> saved;
		if (flatfile_read(directory, since_file, sizeof(int64_t), &saved, error) ==
			    flatfile_read_result::ok &&
		    saved.size() == sizeof(int64_t))
		{
			int64_t since = 0;
			memcpy(&since, saved.data(), sizeof(since));
			if (since > 0 && since <= now)
				return static_cast<time_t>(since);
		}
		if (error && error->empty())
			*error = "no saved cold start time";
		return now;
	}
	if (mkdir(directory.c_str(), 0700) < 0 && errno != EEXIST)
	{
		if (error)
			*error = std::string("create shop sales directory: ") + strerror(errno);
		return now;
	}
	const int64_t since = now;
	std::vector<uint8_t> bytes(sizeof(since));
	memcpy(bytes.data(), &since, sizeof(since));
	flatfile_atomic_write(directory, since_file, bytes, error);
	return now;
}
