#include "recast_bridge.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <vector>

#include "DetourAlloc.h"
#include "DetourCommon.h"
#include "DetourCrowd.h"
#include "DetourNavMesh.h"
#include "DetourNavMeshBuilder.h"
#include "DetourNavMeshQuery.h"
#include "Recast.h"

namespace battlespades {
namespace {

constexpr unsigned short kWalkFlag = 0x01;
constexpr float kCellSize = 0.5f;
constexpr float kCellHeight = 0.25f;
constexpr float kTileWorldSize = 32.0f;

void free_intermediates(
    rcHeightfield* solid,
    rcCompactHeightfield* compact,
    rcContourSet* contours,
    rcPolyMesh* mesh,
    rcPolyMeshDetail* detail
) {
    rcFreeHeightField(solid);
    rcFreeCompactHeightfield(compact);
    rcFreeContourSet(contours);
    rcFreePolyMesh(mesh);
    rcFreePolyMeshDetail(detail);
}

}  // namespace

RecastNavigation::RecastNavigation()
    : nav_mesh_(dtAllocNavMesh()),
      nav_query_(dtAllocNavMeshQuery()),
      crowd_(nullptr),
      tile_count_(0) {
    if (nav_mesh_ == nullptr || nav_query_ == nullptr) {
        return;
    }
    dtNavMeshParams params{};
    params.orig[0] = 0.0f;
    params.orig[1] = -240.0f;
    params.orig[2] = 0.0f;
    params.tileWidth = kTileWorldSize;
    params.tileHeight = kTileWorldSize;
    params.maxTiles = 512;
    // Detour's 32-bit poly refs reserve at least ten salt bits. 512 tiles
    // therefore permits at most 8192 polygons per tile (9 + 13 index bits).
    params.maxPolys = 8192;
    if (dtStatusFailed(nav_mesh_->init(&params))) {
        dtFreeNavMesh(nav_mesh_);
        nav_mesh_ = nullptr;
        return;
    }
    if (dtStatusFailed(nav_query_->init(nav_mesh_, 4096))) {
        dtFreeNavMeshQuery(nav_query_);
        nav_query_ = nullptr;
        return;
    }
    crowd_ = dtAllocCrowd();
    if (crowd_ == nullptr || !crowd_->init(64, 0.5f, nav_mesh_)) {
        dtFreeCrowd(crowd_);
        crowd_ = nullptr;
    } else {
        dtQueryFilter* filter = crowd_->getEditableFilter(0);
        filter->setIncludeFlags(kWalkFlag);
        filter->setExcludeFlags(0);
    }
}

RecastNavigation::~RecastNavigation() {
    dtFreeCrowd(crowd_);
    dtFreeNavMeshQuery(nav_query_);
    dtFreeNavMesh(nav_mesh_);
}

bool RecastNavigation::ready() const {
    return nav_mesh_ != nullptr && nav_query_ != nullptr;
}

bool RecastNavigation::build_tile(
    int tile_x,
    int tile_y,
    const std::vector<float>& vertices,
    const std::vector<int>& triangles,
    const std::vector<float>& bounds_min,
    const std::vector<float>& bounds_max
) {
    if (!ready() || vertices.size() < 9 || vertices.size() % 3 != 0 ||
        triangles.size() < 3 || triangles.size() % 3 != 0 ||
        bounds_min.size() != 3 || bounds_max.size() != 3) {
        return false;
    }

    rcContext context(false);
    rcConfig config{};
    config.cs = kCellSize;
    config.ch = kCellHeight;
    config.walkableSlopeAngle = 50.0f;
    config.walkableHeight = static_cast<int>(std::ceil(2.0f / config.ch));
    config.walkableClimb = static_cast<int>(std::floor(1.05f / config.ch));
    config.walkableRadius = static_cast<int>(std::ceil(0.35f / config.cs));
    config.maxEdgeLen = static_cast<int>(12.0f / config.cs);
    config.maxSimplificationError = 1.3f;
    config.minRegionArea = 8 * 8;
    config.mergeRegionArea = 20 * 20;
    config.maxVertsPerPoly = 6;
    config.tileSize = static_cast<int>(kTileWorldSize / config.cs);
    config.borderSize = config.walkableRadius + 3;
    config.width = config.tileSize + config.borderSize * 2;
    config.height = config.tileSize + config.borderSize * 2;
    config.detailSampleDist = config.cs * 6.0f;
    config.detailSampleMaxError = config.ch;
    rcVcopy(config.bmin, bounds_min.data());
    rcVcopy(config.bmax, bounds_max.data());
    config.bmin[0] -= config.borderSize * config.cs;
    config.bmin[2] -= config.borderSize * config.cs;
    config.bmax[0] += config.borderSize * config.cs;
    config.bmax[2] += config.borderSize * config.cs;

    rcHeightfield* solid = rcAllocHeightfield();
    rcCompactHeightfield* compact = nullptr;
    rcContourSet* contours = nullptr;
    rcPolyMesh* mesh = nullptr;
    rcPolyMeshDetail* detail = nullptr;
    if (solid == nullptr || !rcCreateHeightfield(
            &context,
            *solid,
            config.width,
            config.height,
            config.bmin,
            config.bmax,
            config.cs,
            config.ch)) {
        free_intermediates(solid, compact, contours, mesh, detail);
        return false;
    }

    const int vertex_count = static_cast<int>(vertices.size() / 3);
    const int triangle_count = static_cast<int>(triangles.size() / 3);
    std::vector<unsigned char> areas(triangle_count, RC_NULL_AREA);
    rcMarkWalkableTriangles(
        &context,
        config.walkableSlopeAngle,
        vertices.data(),
        vertex_count,
        triangles.data(),
        triangle_count,
        areas.data());
    if (!rcRasterizeTriangles(
            &context,
            vertices.data(),
            vertex_count,
            triangles.data(),
            areas.data(),
            triangle_count,
            *solid,
            config.walkableClimb)) {
        free_intermediates(solid, compact, contours, mesh, detail);
        return false;
    }
    rcFilterLowHangingWalkableObstacles(&context, config.walkableClimb, *solid);
    rcFilterLedgeSpans(
        &context, config.walkableHeight, config.walkableClimb, *solid);
    rcFilterWalkableLowHeightSpans(&context, config.walkableHeight, *solid);

    compact = rcAllocCompactHeightfield();
    if (compact == nullptr || !rcBuildCompactHeightfield(
            &context,
            config.walkableHeight,
            config.walkableClimb,
            *solid,
            *compact)) {
        free_intermediates(solid, compact, contours, mesh, detail);
        return false;
    }
    rcFreeHeightField(solid);
    solid = nullptr;
    if (!rcErodeWalkableArea(&context, config.walkableRadius, *compact) ||
        !rcBuildLayerRegions(
            &context,
            *compact,
            config.borderSize,
            config.minRegionArea)) {
        free_intermediates(solid, compact, contours, mesh, detail);
        return false;
    }

    contours = rcAllocContourSet();
    if (contours == nullptr || !rcBuildContours(
            &context,
            *compact,
            config.maxSimplificationError,
            config.maxEdgeLen,
            *contours)) {
        free_intermediates(solid, compact, contours, mesh, detail);
        return false;
    }
    mesh = rcAllocPolyMesh();
    if (mesh == nullptr || !rcBuildPolyMesh(
            &context, *contours, config.maxVertsPerPoly, *mesh) ||
        mesh->npolys == 0) {
        free_intermediates(solid, compact, contours, mesh, detail);
        return false;
    }
    detail = rcAllocPolyMeshDetail();
    if (detail == nullptr || !rcBuildPolyMeshDetail(
            &context,
            *mesh,
            *compact,
            config.detailSampleDist,
            config.detailSampleMaxError,
            *detail)) {
        free_intermediates(solid, compact, contours, mesh, detail);
        return false;
    }

    for (int index = 0; index < mesh->npolys; ++index) {
        if (mesh->areas[index] == RC_WALKABLE_AREA) {
            mesh->areas[index] = 0;
        }
        mesh->flags[index] = kWalkFlag;
    }
    dtNavMeshCreateParams params{};
    params.verts = mesh->verts;
    params.vertCount = mesh->nverts;
    params.polys = mesh->polys;
    params.polyAreas = mesh->areas;
    params.polyFlags = mesh->flags;
    params.polyCount = mesh->npolys;
    params.nvp = mesh->nvp;
    params.detailMeshes = detail->meshes;
    params.detailVerts = detail->verts;
    params.detailVertsCount = detail->nverts;
    params.detailTris = detail->tris;
    params.detailTriCount = detail->ntris;
    params.walkableHeight = 2.0f;
    params.walkableRadius = 0.35f;
    params.walkableClimb = 1.05f;
    params.tileX = tile_x;
    params.tileY = tile_y;
    params.tileLayer = 0;
    rcVcopy(params.bmin, mesh->bmin);
    rcVcopy(params.bmax, mesh->bmax);
    params.cs = config.cs;
    params.ch = config.ch;
    params.buildBvTree = true;

    unsigned char* nav_data = nullptr;
    int nav_data_size = 0;
    const bool created = dtCreateNavMeshData(
        &params, &nav_data, &nav_data_size);
    free_intermediates(solid, compact, contours, mesh, detail);
    if (!created || nav_data == nullptr) {
        dtFree(nav_data);
        return false;
    }

    const dtTileRef old_ref = nav_mesh_->getTileRefAt(tile_x, tile_y, 0);
    if (old_ref != 0) {
        nav_mesh_->removeTile(old_ref, nullptr, nullptr);
    }
    dtTileRef new_ref = 0;
    if (dtStatusFailed(nav_mesh_->addTile(
            nav_data,
            nav_data_size,
            DT_TILE_FREE_DATA,
            0,
            &new_ref))) {
        dtFree(nav_data);
        return false;
    }
    if (old_ref == 0) {
        ++tile_count_;
    }
    return new_ref != 0;
}

bool RecastNavigation::remove_tile(int tile_x, int tile_y) {
    if (!ready()) {
        return false;
    }
    const dtTileRef old_ref = nav_mesh_->getTileRefAt(tile_x, tile_y, 0);
    if (old_ref == 0) {
        return true;
    }
    if (dtStatusFailed(nav_mesh_->removeTile(old_ref, nullptr, nullptr))) {
        return false;
    }
    tile_count_ = std::max(0, tile_count_ - 1);
    return true;
}

std::vector<float> RecastNavigation::find_path(
    const std::vector<float>& start,
    const std::vector<float>& end
) const {
    std::vector<float> result;
    if (!ready() || start.size() != 3 || end.size() != 3) {
        return result;
    }
    dtQueryFilter filter;
    filter.setIncludeFlags(kWalkFlag);
    filter.setExcludeFlags(0);
    const float extents[3] = {2.0f, 4.0f, 2.0f};
    dtPolyRef start_ref = 0;
    dtPolyRef end_ref = 0;
    float nearest_start[3]{};
    float nearest_end[3]{};
    if (dtStatusFailed(nav_query_->findNearestPoly(
            start.data(), extents, &filter, &start_ref, nearest_start)) ||
        dtStatusFailed(nav_query_->findNearestPoly(
            end.data(), extents, &filter, &end_ref, nearest_end)) ||
        start_ref == 0 || end_ref == 0) {
        return result;
    }
    dtPolyRef polygon_path[256]{};
    int polygon_count = 0;
    if (dtStatusFailed(nav_query_->findPath(
            start_ref,
            end_ref,
            nearest_start,
            nearest_end,
            &filter,
            polygon_path,
            &polygon_count,
            256)) || polygon_count <= 0) {
        return result;
    }
    float straight_path[256 * 3]{};
    unsigned char straight_flags[256]{};
    dtPolyRef straight_refs[256]{};
    int straight_count = 0;
    if (dtStatusFailed(nav_query_->findStraightPath(
            nearest_start,
            nearest_end,
            polygon_path,
            polygon_count,
            straight_path,
            straight_flags,
            straight_refs,
            &straight_count,
            256)) || straight_count <= 0) {
        return result;
    }
    result.assign(straight_path, straight_path + straight_count * 3);
    return result;
}

std::vector<float> RecastNavigation::crowd_steer(
    int agent_id,
    const std::vector<float>& start,
    const std::vector<float>& end,
    const std::vector<float>& velocity,
    float max_speed,
    float max_acceleration,
    float delta_time
) {
    std::vector<float> result;
    if (!ready() || crowd_ == nullptr || agent_id < 0 || start.size() != 3 ||
        end.size() != 3 || velocity.size() != 3) {
        return result;
    }

    dtQueryFilter filter;
    filter.setIncludeFlags(kWalkFlag);
    filter.setExcludeFlags(0);
    const float extents[3] = {2.0f, 4.0f, 2.0f};
    dtPolyRef start_ref = 0;
    dtPolyRef end_ref = 0;
    float nearest_start[3]{};
    float nearest_end[3]{};
    if (dtStatusFailed(nav_query_->findNearestPoly(
            start.data(), extents, &filter, &start_ref, nearest_start)) ||
        dtStatusFailed(nav_query_->findNearestPoly(
            end.data(), extents, &filter, &end_ref, nearest_end)) ||
        start_ref == 0 || end_ref == 0) {
        return result;
    }

    dtCrowdAgentParams params{};
    params.radius = 0.35f;
    params.height = 2.0f;
    params.maxAcceleration = std::max(0.1f, max_acceleration);
    params.maxSpeed = std::max(0.1f, max_speed);
    params.collisionQueryRange = params.radius * 12.0f;
    params.pathOptimizationRange = params.radius * 30.0f;
    params.separationWeight = 2.0f;
    params.updateFlags = DT_CROWD_ANTICIPATE_TURNS |
                         DT_CROWD_OBSTACLE_AVOIDANCE |
                         DT_CROWD_SEPARATION |
                         DT_CROWD_OPTIMIZE_VIS |
                         DT_CROWD_OPTIMIZE_TOPO;
    params.obstacleAvoidanceType = 0;
    params.queryFilterType = 0;

    int slot = -1;
    const auto existing = crowd_agents_.find(agent_id);
    if (existing != crowd_agents_.end()) {
        slot = existing->second;
    }
    dtCrowdAgent* agent = slot >= 0 ? crowd_->getEditableAgent(slot) : nullptr;
    if (agent == nullptr || !agent->active) {
        slot = crowd_->addAgent(nearest_start, &params);
        if (slot < 0) {
            return result;
        }
        crowd_agents_[agent_id] = slot;
        agent = crowd_->getEditableAgent(slot);
    } else {
        crowd_->updateAgentParameters(slot, &params);
        // Server physics remains authoritative. Re-anchor the crowd proxy to
        // the latest observed body before calculating local avoidance.
        agent->corridor.reset(start_ref, nearest_start);
        dtVcopy(agent->npos, nearest_start);
        dtVcopy(agent->vel, velocity.data());
        agent->boundary.reset();
    }

    if (!crowd_->requestMoveTarget(slot, end_ref, nearest_end)) {
        return result;
    }
    crowd_->update(std::clamp(delta_time, 0.01f, 0.25f), nullptr);
    agent = crowd_->getEditableAgent(slot);
    if (agent == nullptr || !agent->active) {
        return result;
    }
    const float* steering = dtVlenSqr(agent->nvel) > 1e-6f
        ? agent->nvel
        : agent->dvel;
    result.assign(steering, steering + 3);
    return result;
}

void RecastNavigation::remove_crowd_agent(int agent_id) {
    if (crowd_ == nullptr) {
        return;
    }
    const auto existing = crowd_agents_.find(agent_id);
    if (existing == crowd_agents_.end()) {
        return;
    }
    crowd_->removeAgent(existing->second);
    crowd_agents_.erase(existing);
}

int RecastNavigation::tile_count() const {
    return tile_count_;
}

}  // namespace battlespades
