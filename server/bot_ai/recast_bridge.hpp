#pragma once

#include <unordered_map>
#include <vector>

class dtCrowd;
class dtNavMesh;
class dtNavMeshQuery;

namespace battlespades {

class RecastNavigation {
public:
    RecastNavigation();
    ~RecastNavigation();

    RecastNavigation(const RecastNavigation&) = delete;
    RecastNavigation& operator=(const RecastNavigation&) = delete;

    bool ready() const;
    bool build_tile(
        int tile_x,
        int tile_y,
        const std::vector<float>& vertices,
        const std::vector<int>& triangles,
        const std::vector<float>& bounds_min,
        const std::vector<float>& bounds_max
    );
    bool remove_tile(int tile_x, int tile_y);
    std::vector<float> find_path(
        const std::vector<float>& start,
        const std::vector<float>& end
    ) const;
    std::vector<float> crowd_steer(
        int agent_id,
        const std::vector<float>& start,
        const std::vector<float>& end,
        const std::vector<float>& velocity,
        float max_speed,
        float max_acceleration,
        float delta_time
    );
    void remove_crowd_agent(int agent_id);
    int tile_count() const;

private:
    dtNavMesh* nav_mesh_;
    dtNavMeshQuery* nav_query_;
    dtCrowd* crowd_;
    std::unordered_map<int, int> crowd_agents_;
    int tile_count_;
};

}  // namespace battlespades
