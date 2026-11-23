#ifndef DURIS_SHOP_SALES_SINCE_H
#define DURIS_SHOP_SALES_SINCE_H

#include <ctime>
#include <string>

// When the "rather common" sell-price penalty started counting sales. A cold start
// (reboot or crash) starts counting now; a copyover keeps the cold start's time.
time_t shop_sales_since(const std::string &directory, bool copyover, time_t now,
			std::string *error);

#endif
